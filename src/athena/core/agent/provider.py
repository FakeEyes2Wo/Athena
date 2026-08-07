"""Responses 流式 Provider — openai SDK Chat Completions streaming。"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from openai import AsyncOpenAI
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

if TYPE_CHECKING:
    from athena.core.agent.models import AgentConfig
    from athena.core.tool import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamEvent:
    """流式响应事件 — kind 区分文本增量、函数调用、完成和错误四种类型。"""

    kind: Literal["text_delta", "function_call", "response_completed", "error"]
    data: dict[str, Any] = field(default_factory=dict)


class ResponsesProvider:
    """OpenAI-compatible Chat Completions 流式调用。"""

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model_name = model
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def client(self) -> AsyncOpenAI:
        """返回注入的客户端；未注入时按 OpenAI SDK 默认环境变量延迟创建。"""

        if self._client is None:
            self._client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        return self._client

    # TODO: 这里弄一个stream和astream
    async def stream(
        self,
        config: "AgentConfig",
        tools: "ToolRegistry",
        messages: list[ModelMessage],
        cancel: asyncio.Event,
    ) -> AsyncGenerator[StreamEvent, None]:
        api_msgs = _to_api(messages)
        tool_defs = [spec.to_openai_tool() for spec in tools.specs]

        kw: dict = dict(
            model=self.model_name,
            messages=api_msgs,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            stream=True,
        )
        if tool_defs:
            kw["tools"] = tool_defs
            kw["tool_choice"] = "auto"

        stream = await self.client.chat.completions.create(**kw)

        # 流式处理：累积 text delta + 解析 tool call 增量
        bufs: dict[int, dict] = {}
        finish: str = ""
        text = ""

        try:
            async for chunk in stream:
                if cancel.is_set():
                    yield StreamEvent(kind="error", data={"message": "cancelled"})
                    return
                for c in chunk.choices or []:
                    d = c.delta
                    if c.finish_reason:
                        finish = c.finish_reason
                    if d.content:
                        text += d.content
                        yield StreamEvent(
                            kind="text_delta",
                            data={"delta": d.content, "accumulated": text},
                        )
                    if d.tool_calls:
                        for t in d.tool_calls:
                            i = t.index
                            if i not in bufs:
                                bufs[i] = {
                                    "id": t.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            if t.id:
                                bufs[i]["id"] = t.id
                            if t.function:
                                if t.function.name:
                                    bufs[i]["name"] = t.function.name
                                if t.function.arguments:
                                    bufs[i]["arguments"] += t.function.arguments

                # tool_calls finish：组装并发出所有缓冲的函数调用
                if finish == "tool_calls":
                    for i in sorted(bufs):
                        b = bufs[i]
                        if b["id"] and b["name"]:
                            try:
                                args = (
                                    json.loads(b["arguments"]) if b["arguments"] else {}
                                )
                            except json.JSONDecodeError:
                                # LLM 返回了非 JSON 格式的工具参数，视为空参数
                                args = {}
                            yield StreamEvent(
                                kind="function_call",
                                data={
                                    "call_id": b["id"],
                                    "name": b["name"],
                                    "arguments": args,
                                },
                            )
                    finish = ""
                    bufs.clear()

        except Exception as exc:  # 流读取未预期异常 → 返回 error 事件
            logger.error("stream 读取失败: %s", exc)
            yield StreamEvent(
                kind="error", data={"message": f"{type(exc).__name__}: {exc}"}
            )
            return

        yield StreamEvent(
            kind="response_completed",
            data={"finish_reason": finish or "stop", "accumulated_text": text},
        )


def _to_api(msgs: list[ModelMessage]) -> list[dict]:
    """将 PydanticAI ModelMessage 列表转为 OpenAI API dict 格式。

    映射规则：
    - system-prompt → role: "system"
    - user-prompt / text → role: "user"
    - tool-return → role: "tool"（含截断保护）
    - ModelResponse with tool-call → role: "assistant" + tool_calls 块
    - ModelResponse without tool-call → role: "assistant" + 纯文本 content
    """
    out: list[dict] = []
    for m in msgs:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                k = getattr(p, "part_kind", None)
                if k in ("system-prompt", "user-prompt", "text"):
                    out.append(
                        {
                            "role": "system" if k == "system-prompt" else "user",
                            "content": str(getattr(p, "content", "")),
                        }
                    )
                elif k == "tool-return":
                    c = str(getattr(p, "content", ""))
                    if len(c) > 50000:
                        c = c[:24950] + "\n...[TRUNCATED]...\n" + c[-24950:]
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(getattr(p, "tool_call_id", "")),
                            "content": c,
                        }
                    )
        elif isinstance(m, ModelResponse):
            parts = list(m.parts)
            tc = [p for p in parts if getattr(p, "part_kind", None) == "tool-call"]
            if tc:
                out.append(
                    {
                        "role": "assistant",
                        "content": "".join(
                            str(getattr(p, "content", ""))
                            for p in parts
                            if getattr(p, "part_kind", None) == "text"
                        )
                        or None,
                        "tool_calls": [
                            {
                                "id": str(getattr(p, "tool_call_id", "")),
                                "type": "function",
                                "function": {
                                    "name": str(getattr(p, "tool_name", "")),
                                    "arguments": str(getattr(p, "args", "{}")),
                                },
                            }
                            for p in tc
                        ],
                    }
                )
            else:
                out.append(
                    {
                        "role": "assistant",
                        "content": "".join(
                            str(getattr(p, "content", ""))
                            for p in parts
                            if hasattr(p, "content")
                        ),
                    }
                )
    return out

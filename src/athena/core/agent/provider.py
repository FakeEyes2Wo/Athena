"""Responses 流式 Provider — openai SDK Chat Completions streaming。"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from openai import AsyncOpenAI, BadRequestError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

from athena.core.agent import settings
from athena.core.tool_types import truncate_text

if TYPE_CHECKING:
    from athena.core.agent.models import AgentConfig
    from athena.core.tool import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamEvent:
    """流式响应事件 — kind 区分文本增量、函数调用、完成和错误四种类型。"""

    kind: Literal["text_delta", "function_call", "response_completed", "error"]
    data: dict[str, Any] = field(default_factory=dict)


_DSML_KINDS = ("tool_use_error", "tool_calls", "tool_call", "function_calls")
_DSML_BARS = ("|", "｜")

_DSML_OPEN_TOKENS = tuple(
    f"<{bar}DSML{bar}{kind}>" for bar in _DSML_BARS for kind in _DSML_KINDS
)
_DSML_CLOSE_TOKENS = tuple(
    f"</{bar}DSML{bar}{kind}>" for bar in _DSML_BARS for kind in _DSML_KINDS
)


class _DeepSeekTextFilter:
    """剥离 deepseek 的 DSML tool-call 传输语法，防止泄漏进可见文本。

    DeepSeek 在 ``tool_choice=auto`` + 流式下会间歇性把 ``<｜DSML｜tool_calls>``
    等标记片段泄漏进 ``content``，污染结构化输出（json_object）。原生
    ``delta.tool_calls`` 仍是权威工具调用来源；本过滤器只负责清理文本，跨
    chunk 边界缓冲 open/close token 以防误删。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def push(self, chunk: str) -> str:
        """喂入一段增量，返回可安全发出的干净文本。"""
        self._buffer += chunk
        return self._consume(final=False)

    def flush(self) -> str:
        """结束流：返回剩余干净文本，丢弃未闭合的 DSML 块。"""
        return self._consume(final=True)

    def _consume(self, *, final: bool) -> str:
        out: list[str] = []
        max_open = max(len(t) for t in _DSML_OPEN_TOKENS)
        max_close = max(len(t) for t in _DSML_CLOSE_TOKENS)
        while self._buffer:
            if self._inside:
                close = self._find_earliest(self._buffer, _DSML_CLOSE_TOKENS)
                if close is not None:
                    index, token = close
                    self._buffer = self._buffer[index + len(token) :]
                    self._inside = False
                    continue
                keep = 0 if final else min(len(self._buffer), max_close - 1)
                self._buffer = self._buffer[len(self._buffer) - keep :]
                if final:
                    self._inside = False
                return "".join(out)
            opened = self._find_earliest(self._buffer, _DSML_OPEN_TOKENS)
            if opened is not None:
                index, token = opened
                if index:
                    out.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(token) :]
                self._inside = True
                continue
            if final:
                out.append(self._buffer)
                self._buffer = ""
                return "".join(out)
            emit_len = len(self._buffer) - min(len(self._buffer), max_open - 1)
            if emit_len <= 0:
                return "".join(out)
            out.append(self._buffer[:emit_len])
            self._buffer = self._buffer[emit_len:]
            return "".join(out)
        return "".join(out)

    @staticmethod
    def _find_earliest(text: str, tokens: tuple[str, ...]) -> tuple[int, str] | None:
        best: tuple[int, str] | None = None
        for token in tokens:
            index = text.find(token)
            if index != -1 and (best is None or index < best[0]):
                best = (index, token)
        return best


class BaseProvider(ABC):
    """LLM provider 稳定接口:模型名、client、流式 stream()。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """当前模型名。"""

    @property
    @abstractmethod
    def client(self) -> AsyncOpenAI:
        """底层客户端(注入或按 settings 构造)。"""

    @abstractmethod
    async def stream(
        self,
        config: "AgentConfig",
        tools: "ToolRegistry",
        messages: list[ModelMessage],
        cancel: asyncio.Event,
        *,
        output_type: type | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """流式调用 LLM,产出文本增量 / 工具调用 / 完成 / 错误事件。"""


class ResponsesProvider(BaseProvider):
    """OpenAI 兼容 Chat Completions 流式 provider。

    ``provider_kind`` 决定结构化输出的适配方式:
    - ``openai``: ``response_format={"type": "json_schema", ...}``(OpenAI 原生)。
    - ``deepseek``: ``{"type": "json_object"}`` + 把 output_type 完整 schema 注入
      prompt(DeepSeek 不支持 json_schema)。
    缺省取 ``settings.provider_kind()``(仓库默认 deepseek),也可显式注入用于测试。
    """

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
        provider_kind: str | None = None,
    ) -> None:
        self._model_name = model
        self._client = client
        self.provider_kind = provider_kind or settings.provider_kind()

    @property
    def model_name(self) -> str:
        """当前模型名。"""
        return self._model_name

    @property
    def client(self) -> AsyncOpenAI:
        """返回注入的 client；未注入时按 settings（.env 的 DeepSeek 凭据）构造。"""
        if self._client is None:
            self._client = settings.get_client()
        return self._client

    def _response_format(self, output_type: type) -> dict[str, Any] | None:
        """按 provider 能力返回 response_format;不支持的 provider 返回 None。

        - openai: json_schema 结构化输出(OpenAI 原生)。
        - deepseek: json_object(合法 JSON 但无 schema 强制;schema 由
          ``_schema_instruction`` 注入 prompt)。
        """
        if self.provider_kind == "openai":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": output_type.__name__,
                    "schema": output_type.model_json_schema(),
                },
            }
        if self.provider_kind in ("deepseek", "qwen"):
            return {"type": "json_object"}
        return None

    def _extra_body(self) -> dict[str, Any]:
        """按后端返回 ``extra_body``；不认识的字段一律不发。

        这里曾经无条件发 ``{"thinking": {"type": "disabled"}}``。那是 DeepSeek 的
        字段名：OpenAI 对未知请求参数直接回 400，DashScope 认的也是另一个名字
        (``enable_thinking``)。于是"关思考"这个本意只在 DeepSeek 上成立，另外两个
        后端要么报错要么被无视。字段名必须跟着后端走。
        """
        thinking = settings.enable_thinking()
        if self.provider_kind == "deepseek":
            return {"thinking": {"type": "enabled" if thinking else "disabled"}}
        if self.provider_kind == "qwen":
            return {"enable_thinking": thinking}
        return {}

    @staticmethod
    def _assemble_function_calls(bufs: dict[int, dict]) -> list[StreamEvent]:
        """把缓冲的工具调用按 index 顺序组装为 function_call 事件。"""
        events: list[StreamEvent] = []
        for i in sorted(bufs):
            b = bufs[i]
            if not (b["id"] and b["name"]):
                continue
            try:
                args = json.loads(b["arguments"]) if b["arguments"] else {}
            except json.JSONDecodeError:
                # LLM 返回了非 JSON 格式的工具参数，视为空参数
                args = {}
            events.append(
                StreamEvent(
                    kind="function_call",
                    data={"call_id": b["id"], "name": b["name"], "arguments": args},
                )
            )
        return events

    async def stream(
        self,
        config: "AgentConfig",
        tools: "ToolRegistry",
        messages: list[ModelMessage],
        cancel: asyncio.Event,
        *,
        output_type: type | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """流式调用 Chat Completions，产出文本增量 / 工具调用 / 完成事件。

        ``response_format`` 与工具互斥，二者只能取一。它把整条回复约束成 schema，
        模型于是一个 tool call 也发不出来；而 Athena 每个 Agent 都带 ``output_type``，
        真跑起来就是 evaluator 连着十几轮回 ``{"decision":"continue","reason":"Let me
        read the training CSV"}``、一次工具都不调，PREPARE 因此永远冻结不出 evaluator。
        有工具时改把 schema 注入 prompt（``_schema_instruction``，原本只有 DeepSeek 走
        这条），让 ReAct 先照常用工具、末轮再回 JSON；无工具时才用严格 schema。
        """
        api_msgs = _to_api(messages)
        tool_defs = [spec.to_openai_tool() for spec in tools.specs]
        if output_type is not None and (
            tool_defs or self.provider_kind in ("deepseek", "qwen")
        ):
            api_msgs = [*api_msgs, _schema_instruction(output_type)]

        kw: dict = {
            "model": self.model_name,
            "messages": api_msgs,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "stream": True,
        }
        extra_body = self._extra_body()
        if extra_body:
            kw["extra_body"] = extra_body
        # 只有显式配置了种子才发送：不认这个参数的后端会因未知字段直接 400。
        if config.seed is not None:
            kw["seed"] = config.seed
        if output_type is not None and not tool_defs:
            response_format = self._response_format(output_type)
            if response_format is not None:
                kw["response_format"] = response_format
        if tool_defs:
            kw["tools"] = tool_defs
            kw["tool_choice"] = config.tool_choice

        try:
            stream = await self.client.chat.completions.create(**kw)
        except BadRequestError as exc:
            if not _response_format_unavailable(exc):
                raise
            logger.warning(
                "provider does not support json_schema response_format; "
                "retrying with prompt-guided JSON"
            )
            kw.pop("response_format", None)
            stream = await self.client.chat.completions.create(**kw)

        # 流式处理：累积 text delta + 解析 tool call 增量
        bufs: dict[int, dict] = {}
        finish: str = ""
        text = ""
        dsml_filter = (
            _DeepSeekTextFilter() if self.provider_kind == "deepseek" else None
        )

        def release_filtered_tail() -> "StreamEvent | None":
            """放出 DSML 过滤器扣留的尾部文本，无残留时返回 None。

            过滤器为了不把跨 chunk 的 ``<｜DSML｜…>`` 标记劈开，每次 push 都扣留
            最后若干字符；这些字符必须在消息边界（function_call / 流结束）之前
            放出来，否则消费方看到的每条 agent 文本都会少一截尾巴。
            """
            nonlocal text
            if dsml_filter is None:
                return None
            tail = dsml_filter.flush()
            if not tail:
                return None
            text += tail
            return StreamEvent(
                kind="text_delta", data={"delta": tail, "accumulated": text}
            )

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
                        delta = (
                            dsml_filter.push(d.content)
                            if dsml_filter is not None
                            else d.content
                        )
                        if delta:
                            text += delta
                            yield StreamEvent(
                                kind="text_delta",
                                data={"delta": delta, "accumulated": text},
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

                # tool_calls finish：组装并发出所有缓冲的函数调用。
                # function_call 是下游投影层的消息边界，先把扣留的文本尾部放出去，
                # 否则这条 agent 消息会缺尾，尾巴还会粘到下一条消息头上。
                if finish == "tool_calls":
                    pending = release_filtered_tail()
                    if pending is not None:
                        yield pending
                    for event in self._assemble_function_calls(bufs):
                        yield event
                    finish = ""
                    bufs.clear()

        except Exception as exc:  # 流读取未预期异常 → 返回 error 事件
            logger.error("stream 读取失败: %s", exc)
            yield StreamEvent(
                kind="error", data={"message": f"{type(exc).__name__}: {exc}"}
            )
            return

        pending = release_filtered_tail()
        if pending is not None:
            yield pending

        # 流结束仍残留缓冲的工具调用（finish 非 tool_calls，如 length 截断或空尾部
        # chunk）——不能静默丢弃，否则 agent 空转成功或报误导性的结构化输出错误。
        for event in self._assemble_function_calls(bufs):
            yield event

        yield StreamEvent(
            kind="response_completed",
            data={"finish_reason": finish or "stop", "accumulated_text": text},
        )


class OpenAIProvider(ResponsesProvider):
    """OpenAI 后端:json_schema 结构化输出。"""

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(model, client=client, provider_kind="openai")


class DeepSeekProvider(ResponsesProvider):
    """DeepSeek 后端:json_object + schema 注入 prompt。"""

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(model, client=client, provider_kind="deepseek")


class QwenProvider(ResponsesProvider):
    """通义千问(阿里云百炼 DashScope 兼容模式)后端。

    兼容模式说的就是 OpenAI Chat Completions 协议,所以流式、工具调用与
    ``json_object`` 都能直接复用 ``ResponsesProvider``。按 DeepSeek 而不是 OpenAI
    的档位适配结构化输出:DashScope 兼容端点对 ``json_schema`` 的支持随模型而变,
    ``json_object`` + schema 注入 prompt 是各型号都成立的那一档。
    """

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(model, client=client, provider_kind="qwen")


_ANTHROPIC_NOT_IMPLEMENTED = (
    "native Anthropic provider is not yet implemented; "
    "set LLM_PROVIDER=deepseek|openai"
)


class AnthropicProvider(BaseProvider):
    """原生 Anthropic provider 保留槽位(本轮未实现)。

    构造即抛清晰错误,避免静默走错路径。原生实现需要 ``anthropic`` SDK 与
    ``messages.create``/``output_schema`` 的独立流式形态,留作后续任务。
    抽象成员 stub 仅用于通过 ABC 实例化检查;正常构造在 ``__init__`` 即抛错。
    """

    def __init__(
        self,
        model: str,
        *,
        client: object | None = None,
    ) -> None:
        raise NotImplementedError(_ANTHROPIC_NOT_IMPLEMENTED)

    @property
    def model_name(self) -> str:
        raise NotImplementedError(_ANTHROPIC_NOT_IMPLEMENTED)

    @property
    def client(self) -> AsyncOpenAI:
        raise NotImplementedError(_ANTHROPIC_NOT_IMPLEMENTED)

    async def stream(
        self,
        config: "AgentConfig",
        tools: "ToolRegistry",
        messages: list[ModelMessage],
        cancel: asyncio.Event,
        *,
        output_type: type | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        raise NotImplementedError(_ANTHROPIC_NOT_IMPLEMENTED)


def create_provider(
    model: str,
    *,
    client: AsyncOpenAI | None = None,
) -> BaseProvider:
    """按 ``settings.provider_kind()``(LLM_PROVIDER 环境变量)构造对应 provider。

    仅显式选择,不做 base_url/model 前缀推断。
    """
    kind = settings.provider_kind()
    if kind == "openai":
        return OpenAIProvider(model, client=client)
    if kind == "deepseek":
        return DeepSeekProvider(model, client=client)
    if kind == "qwen":
        return QwenProvider(model, client=client)
    if kind == "anthropic":
        return AnthropicProvider(model, client=client)
    raise ValueError(f"unsupported LLM_PROVIDER={kind!r}")


def _schema_instruction(output_type: type) -> dict[str, str]:
    """构造把 output_type 完整 schema 注入 prompt 的 system 消息。

    DeepSeek 的 ``json_object`` 模式不会把 schema 传给模型,必须显式放进
    prompt;消息含 "JSON" 字样,同时满足 DeepSeek 要求 prompt 含 "json" 的前置条件。
    """
    return {
        "role": "system",
        "content": (
            "Return a JSON object matching this schema:\n"
            f"{json.dumps(output_type.model_json_schema(), ensure_ascii=False)}"
        ),
    }


def _response_format_unavailable(exc: BadRequestError) -> bool:
    message = str(exc).lower()
    return (
        exc.status_code == 400
        and "response_format" in message
        and ("unavailable" in message or "unsupported" in message)
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
                    c = truncate_text(str(getattr(p, "content", "")))
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

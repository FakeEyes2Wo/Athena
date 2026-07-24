"""上下文压缩 — 将早期对话历史替换为 LLM 生成的摘要。

对齐 Codex ``compact.rs`` 本地总结模式。
"""

from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart

from athena.memory.context_manager import ContextManager

_SUMMARY_PART_CHARS = 300


@dataclass(slots=True)
class Compaction:
    """一次压缩的结果，包含回滚所需的原始消息。"""

    version: int
    summary: str
    original_items: list[ModelMessage]


class Compactor:
    """将早期对话历史替换为 LLM 生成的摘要。

    Args:
        keep_recent: 保留最近多少 Token 的消息不被压缩。
        summary_model: 用于生成摘要的轻量模型 ID。
    """

    __slots__ = ("_keep_recent", "_summary_model")

    def __init__(self, keep_recent: int = 20_000, summary_model: str = "haiku") -> None:
        if keep_recent < 0:
            raise ValueError("keep_recent 必须为非负数")
        self._keep_recent = keep_recent
        self._summary_model = summary_model

    def should_compact(self, ctx: ContextManager, at_tokens: int = 170_000) -> bool:
        return ctx.tokens >= at_tokens

    async def compact(self, ctx: ContextManager, llm: Any) -> Compaction:
        """执行压缩，原地修改 *ctx*。检查版本号防止并发修改。"""
        items = ctx.items
        split = self._split_recent(items)
        old = items[:split]
        if not old:
            return Compaction(version=ctx.version, summary="", original_items=[])

        source_version = ctx.version
        summary = await self._summarize(old, llm)
        if ctx.version != source_version:
            raise RuntimeError("压缩过程中上下文被并发修改")

        summary_msg = ModelRequest(
            parts=[SystemPromptPart(content=f"[HISTORY SUMMARY]\n{summary}")]
        )
        ctx.replace_range(0, split, [summary_msg])
        return Compaction(version=ctx.version, summary=summary, original_items=old)

    # ── 内部 ───────────────────────────────────────────────────────

    def _split_recent(self, items: list[ModelMessage]) -> int:
        """从后往前累积 Token，找到近期消息的起始位置。"""
        acc = 0
        for i in range(len(items) - 1, -1, -1):
            acc += ContextManager._estimate_one(items[i])
            if acc >= self._keep_recent:
                return i
        return 0

    async def _summarize(self, items: list[ModelMessage], llm: Any) -> str:
        """构造摘要 prompt，调用 LLM 生成摘要。"""
        parts = [
            "Summarize concisely (decisions, findings, code changes, "
            "hypotheses, results):\n\n"
        ]
        for msg in items:
            role = "USER" if isinstance(msg, ModelRequest) else "ASSISTANT"
            for part in msg.parts:
                c = getattr(part, "content", None)
                if isinstance(c, str):
                    parts.extend(("[", role, "] ", c[:_SUMMARY_PART_CHARS], "\n"))
                elif getattr(part, "part_kind", None) == "tool-call":
                    a = getattr(part, "args", None)
                    a_str = str(a) if a is not None else ""
                    parts.extend(
                        (
                            "[ASSISTANT TOOL] ",
                            str(getattr(part, "tool_name", "tool")),
                            " ",
                            a_str[:_SUMMARY_PART_CHARS],
                            "\n",
                        )
                    )

        response = await llm.messages.create(
            model=self._summary_model,
            temperature=0.1,
            max_tokens=2_000,
            messages=[{"role": "user", "content": "".join(parts)}],
        )
        # Anthropic 格式 (content[0].text)；fallback 到 OpenAI 格式或纯字符串
        content = getattr(response, "content", None)
        if isinstance(content, str):
            return content
        if content and hasattr(content[0], "text"):
            return content[0].text
        choices = getattr(response, "choices", None)
        if choices:
            return getattr(choices[0].message, "content", "")
        raise ValueError("摘要模型未返回文本")

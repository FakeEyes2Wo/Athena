"""Projection records and Agent journal forwarding for runtime subscribers."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, new_id

_TERMINAL_EVENT_KINDS = {"turn_completed", "turn_failed", "turn_interrupted"}

PublishEvent = Callable[[str, str, dict | None], Awaitable[None] | None]
SequenceSink = Callable[[int], None]

_PREVIEW_BYTES = 512
_ANSI_STRING = re.compile(
    r"(?:\x1b(?:\]|P|X|\^|_)|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)",
    re.DOTALL,
)
_ANSI_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ANSI_ESCAPE = re.compile(r"\x1b[ -/]*[0-~]")
_UNPRINTABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f�]")
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b\s*[=:]\s*)"
        r"([^\s,;]+)"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


class _TextStore(Protocol):
    async def put_text(self, text: str) -> ArtifactRef:
        """Persist text and return its immutable artifact reference."""
        ...


class OutputEvent(BaseModel):
    """One ordered append-only display record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["output"] = "output"
    seq: int = Field(ge=1)
    # 一条显示消息的身份：同一条 agent 文本的所有流式 delta 与最终落盘记录共用一个
    # id，订阅方据此 upsert，而不是靠"上一条是不是同 plan"猜测该不该合并。升级前
    # 写下的 transcript 没有这个字段，回放时为 None，消费方按 seq 兜底。
    message_id: str | None = None
    session_id: str | None = None
    scope: str | None = None
    scope_id: str | None = None
    source: Literal["supervisor", "agent", "tool"]
    channel: Literal["text", "stdout", "stderr", "error"]
    text: str
    plan: str | None = None
    tool: str | None = None
    artifact_ref: ArtifactRef | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def validate_scope_metadata(self) -> "OutputEvent":
        values = (self.session_id, self.scope, self.scope_id)
        if not (
            all(value is None for value in values)
            or all(isinstance(value, str) and value.strip() for value in values)
        ):
            raise ValueError("output scope metadata must be all present or all absent")
        return self


class StateEvent(BaseModel):
    """One complete replaceable runtime snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["state"] = "state"
    status: str
    phase: str
    plans: list[dict[str, Any]]
    search: dict[str, Any]
    sota: dict[str, Any] | None
    waiting: dict[str, Any] | None
    manual: bool = False
    pending: list[dict[str, Any]] = Field(default_factory=list)
    # VALIDATE 结果与 EDA 目录路径（与 state.json 同源，供订阅方/续跑快照观测）。
    validation: dict[str, Any] | None = None
    eda_dir: str | None = None
    # Supervisor 结构化任务理解（GUI 意图预览据此更新）。
    task_understanding: dict[str, Any] | None = None
    resume_available: bool = False
    resume_reason: str | None = None


def redact(text: str) -> str:
    """Mask secret material in display and shared evidence text."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def sanitize_terminal_text(text: str) -> str:
    """Remove terminal controls while retaining readable Unicode and layout."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ANSI_STRING.sub("", normalized)
    normalized = _ANSI_CSI.sub("", normalized)
    normalized = _ANSI_ESCAPE.sub("", normalized)
    return _UNPRINTABLE.sub("", normalized)


def truncate_middle(text: str, max_chars: int) -> str:
    """Middle-truncate ``text`` to roughly ``max_chars`` while preserving both ends.

    Mirrors codex's ``truncate_middle_chars``: keep the head and tail and replace
    the middle with a ``…N chars truncated…`` marker, so a long shell command stays
    readable (program + first flags on the left, paths/targets on the right)
    without flooding the transcript. Character-counted because it targets terminal
    display rather than an LLM byte budget.
    """
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    left = max_chars // 2
    right = max_chars - left
    removed = len(text) - max_chars
    return f"{text[:left]}…{removed} chars truncated…{text[len(text) - right :]}"


class EventProjector:
    """Create safe runtime records while retaining full tool output as artifacts."""

    def __init__(self, store: _TextStore) -> None:
        self._store = store
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def next_sequence(self) -> int:
        """Allocate the next output sequence number."""
        return self._next_sequence()

    def resume(self, sequence: int) -> None:
        """Resume the sequence counter past replayed history.

        Called on restart after replaying persisted output records, so newly
        projected events continue from ``sequence + 1`` instead of colliding
        with (and being deduplicated against) the restored TUI history.
        """
        self._sequence = max(self._sequence, sequence)

    def output(
        self,
        *,
        source: Literal["supervisor", "agent", "tool"],
        channel: Literal["text", "stdout", "stderr", "error"],
        text: str,
        plan: str | None = None,
        tool: str | None = None,
        artifact_ref: ArtifactRef | None = None,
        truncated: bool = False,
        message_id: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> OutputEvent:
        """Project one already classified display record.

        调用方传入 ``message_id`` 把同一条消息的多次投影绑在一起；不传则这条记录
        自成一条消息。
        """
        return OutputEvent(
            seq=self._next_sequence(),
            message_id=message_id or new_id("msg"),
            session_id=session_id,
            scope=scope,
            scope_id=scope_id,
            source=source,
            channel=channel,
            text=redact(text),
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
            truncated=truncated,
        )

    def text(
        self,
        text: str,
        *,
        source: Literal["supervisor", "agent"] = "agent",
        plan: str | None = None,
    ) -> OutputEvent:
        """Project redacted supervisor or Agent text."""
        return self.output(source=source, channel="text", text=text, plan=plan)

    async def tool_output(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        tool: str | None = None,
        plan: str | None = None,
        artifact_ref: ArtifactRef | None = None,
    ) -> OutputEvent:
        """Project a stderr-first bounded preview and spill full redacted output."""
        safe_stderr = redact(sanitize_terminal_text(stderr))
        safe_stdout = redact(sanitize_terminal_text(stdout))
        full = "\n".join(part for part in (safe_stderr, safe_stdout) if part)
        encoded = full.encode("utf-8")
        truncated = len(encoded) > _PREVIEW_BYTES
        preview = (
            encoded[:_PREVIEW_BYTES].decode("utf-8", errors="ignore")
            if truncated
            else full
        )
        if truncated and artifact_ref is None:
            artifact_ref = await self._store.put_text(full)
        channel: Literal["stdout", "stderr"] = "stderr" if safe_stderr else "stdout"
        return OutputEvent(
            seq=self._next_sequence(),
            message_id=new_id("msg"),
            source="tool",
            channel=channel,
            text=preview,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
            truncated=truncated,
        )


async def forward_run_events(
    agents: AgentRuntime,
    run_id: str,
    publish: PublishEvent,
    after_sequence: int = 0,
    on_sequence: SequenceSink | None = None,
) -> None:
    """Forward one Agent journal through its terminal event."""
    async for event in agents.run_events(run_id, after_sequence=after_sequence):
        await publish(event.kind, event.event_ref, event.data)
        if on_sequence is not None:
            on_sequence(event.sequence)
        if event.kind in _TERMINAL_EVENT_KINDS:
            return


async def wait_run_events(
    agents: AgentRuntime,
    run_id: str,
    publish: PublishEvent | None,
    after_sequence: int = 0,
    on_sequence: SequenceSink | None = None,
):
    """Wait for an Agent run and optionally forward its journal."""
    if publish is None:
        return await agents.wait_run(run_id)
    events = asyncio.create_task(
        forward_run_events(agents, run_id, publish, after_sequence, on_sequence)
    )
    wait = asyncio.create_task(agents.wait_run(run_id))
    try:
        summary = await wait
        await events
        return summary
    finally:
        for task in (events, wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(events, wait, return_exceptions=True)

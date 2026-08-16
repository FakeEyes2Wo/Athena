"""Projection records for the runtime's output/state subscriber protocol."""

import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from athena.core.contracts import ArtifactRef

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
    source: Literal["supervisor", "agent", "tool"]
    channel: Literal["text", "stdout", "stderr", "error"]
    text: str
    plan: str | None = None
    tool: str | None = None
    artifact_ref: ArtifactRef | None = None
    truncated: bool = False


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


def _utf8_prefix(text: str, byte_limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


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
    return f"{text[:left]}…{removed} chars truncated…{text[len(text) - right:]}"


class EventProjector:
    """Create safe runtime records while retaining full tool output as artifacts."""

    def __init__(self, store: _TextStore) -> None:
        self._store = store
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

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
    ) -> OutputEvent:
        """Project one already classified display record."""
        return OutputEvent(
            seq=self._next_sequence(),
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
        preview = _utf8_prefix(full, _PREVIEW_BYTES)
        truncated = len(full.encode("utf-8")) > _PREVIEW_BYTES
        if truncated and artifact_ref is None:
            artifact_ref = await self._store.put_text(full)
        channel: Literal["stdout", "stderr"] = "stderr" if safe_stderr else "stdout"
        return OutputEvent(
            seq=self._next_sequence(),
            source="tool",
            channel=channel,
            text=preview,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
            truncated=truncated,
        )

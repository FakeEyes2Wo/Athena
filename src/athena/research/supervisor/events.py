"""Projection records for the runtime's output/state subscriber protocol."""

import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from athena.core.contracts import ArtifactRef

_PREVIEW_BYTES = 512
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


def redact(text: str) -> str:
    """Mask secret material in display and shared evidence text."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _utf8_prefix(text: str, byte_limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


class EventProjector:
    """Create safe runtime records while retaining full tool output as artifacts."""

    def __init__(self, store: _TextStore) -> None:
        self._store = store
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

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
        safe_stderr = redact(stderr)
        safe_stdout = redact(stdout)
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

"""Pure command matching and durable-state eligibility for task resumption."""

from dataclasses import dataclass
from typing import Literal

ResumeReason = Literal[
    "already_running",
    "paused",
    "failed",
    "interrupted",
    "no_task",
    "stopped",
    "completed",
]


@dataclass(frozen=True)
class ResumeCapability:
    available: bool
    reason: ResumeReason


class ResearchControlError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.retryable = False
        super().__init__(f"{code}: {message}")


def is_continue_command(text: str) -> bool:
    return text.strip().casefold() == "continue"


def resume_capability(state: object) -> ResumeCapability:
    """Classify whether persisted state can resume without inspecting live tasks."""
    status = getattr(state, "status", None)
    phase = getattr(state, "phase", None)

    if status == "COMPLETED" or phase == "COMPLETED":
        return ResumeCapability(False, "completed")
    if status == "STOPPED":
        return ResumeCapability(False, "stopped")

    has_task = bool(
        getattr(state, "task_text", None) or getattr(state, "task_understanding", None)
    )
    if not has_task:
        return ResumeCapability(False, "no_task")

    if status == "RUNNING":
        return ResumeCapability(False, "already_running")
    if status == "WAITING":
        return ResumeCapability(True, "paused")
    if status == "FAILED":
        return ResumeCapability(True, "failed")
    return ResumeCapability(True, "interrupted")

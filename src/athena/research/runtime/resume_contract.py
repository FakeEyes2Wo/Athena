"""Pure command matching and durable-state eligibility for task resumption."""

from dataclasses import dataclass
from typing import Literal

from athena.research.supervisor.state import ResearchState

_ResumeReason = Literal[
    "already_running",
    "paused",
    "failed",
    "interrupted",
    "no_task",
    "stopped",
    "completed",
]


@dataclass(frozen=True, slots=True)
class ResumeCapability:
    reason: _ResumeReason

    @property
    def available(self) -> bool:
        return self.reason in {"paused", "failed", "interrupted"}


class ResearchControlError(RuntimeError):
    retryable = False

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def is_continue_command(text: str) -> bool:
    return text.strip().casefold() == "continue"


def resume_capability(state: ResearchState) -> ResumeCapability:
    """Classify whether persisted state can resume without inspecting live tasks."""
    status = state.status

    if status == "COMPLETED" or state.phase == "COMPLETED":
        return ResumeCapability("completed")
    if status == "STOPPED":
        return ResumeCapability("stopped")

    has_task = bool(state.task_text or state.task_understanding)
    if not has_task:
        return ResumeCapability("no_task")

    if status == "RUNNING":
        return ResumeCapability("already_running")
    if status == "WAITING":
        return ResumeCapability("paused")
    if status == "FAILED":
        return ResumeCapability("failed")
    return ResumeCapability("interrupted")

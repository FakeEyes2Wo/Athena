import pytest

from athena.research.runtime.resume_contract import (
    ResearchControlError,
    ResumeCapability,
    is_continue_command,
    resume_capability,
)
from athena.research.supervisor.state import ResearchState


@pytest.mark.parametrize("text", ["continue", " Continue ", "CONTINUE\n"])
def test_exact_continue_is_a_resume_command(text: str) -> None:
    assert is_continue_command(text) is True


@pytest.mark.parametrize(
    "text",
    ["", "/resume", "continue research", "continue three more attempts", "继续"],
)
def test_non_exact_continue_text_keeps_its_existing_meaning(text: str) -> None:
    assert is_continue_command(text) is False


@pytest.mark.parametrize(
    ("status", "phase", "has_task", "available", "reason"),
    [
        ("RUNNING", "SEARCH", True, False, "already_running"),
        ("WAITING", "PREPARE", True, True, "paused"),
        ("FAILED", "SEARCH", True, True, "failed"),
        ("FAILED", "SEARCH", False, False, "no_task"),
        ("IDLE", "PREPARE", True, True, "interrupted"),
        ("IDLE", "SEARCH", True, True, "interrupted"),
        ("IDLE", "VALIDATE", True, True, "interrupted"),
        ("IDLE", "PREPARE", False, False, "no_task"),
        ("RUNNING", "SEARCH", False, False, "no_task"),
        ("STOPPED", "SEARCH", True, False, "stopped"),
        ("COMPLETED", "COMPLETED", True, False, "completed"),
        ("RUNNING", "COMPLETED", True, False, "completed"),
    ],
)
def test_resume_capability_matrix(
    status: str,
    phase: str,
    has_task: bool,
    available: bool,
    reason: str,
) -> None:
    state = ResearchState(
        status=status,
        phase=phase,
        search_limit=3,
        concurrency=1,
    )
    if has_task:
        state.task_text = "original task"

    capability = resume_capability(state)

    assert capability == ResumeCapability(reason)
    assert capability.available is available


def test_legacy_understanding_without_task_text_is_resumable() -> None:
    state = ResearchState(
        status="IDLE",
        phase="SEARCH",
        search_limit=3,
        concurrency=1,
        task_understanding={"title": "legacy task"},
    )

    assert resume_capability(state) == ResumeCapability("interrupted")


def test_research_control_error_exposes_stable_metadata() -> None:
    error = ResearchControlError(
        "resume_unavailable", "there is no interrupted task to continue"
    )

    assert error.code == "resume_unavailable"
    assert error.retryable is False
    assert str(error) == "resume_unavailable: there is no interrupted task to continue"

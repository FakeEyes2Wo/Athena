import pytest

from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentMessage,
    AgentSpec,
    AgentStatus,
    ErrorCode,
    ForkPolicy,
    ReturnWhen,
    RunStatus,
)

from ._support import EchoRunner, JsonCodec


def test_run_status_enum_values_match_spec() -> None:
    assert [s.value for s in RunStatus] == [
        "queued",
        "running",
        "completed",
        "failed",
        "interrupted",
    ]


def test_agent_status_enum_values_match_spec() -> None:
    assert [s.value for s in AgentStatus] == [
        "starting",
        "idle",
        "running",
        "error",
        "closed",
    ]


def test_terminal_set_contains_only_run_terminal_statuses() -> None:
    assert TERMINAL_RUN_STATUSES == frozenset(
        {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}
    )


def test_interrupted_is_run_terminal_but_not_agent_terminal() -> None:
    assert RunStatus.INTERRUPTED in TERMINAL_RUN_STATUSES
    assert AgentStatus.CLOSED not in TERMINAL_RUN_STATUSES


def test_return_when_has_both_modes() -> None:
    assert {r.value for r in ReturnWhen} == {"first_completed", "all_completed"}


def test_fork_policy_has_exactly_three_forms() -> None:
    assert ForkPolicy.none().mode == "none"
    assert ForkPolicy.full().mode == "full"
    assert ForkPolicy.last_n(3).turns == 3


def test_fork_policy_rejects_nonpositive_turns() -> None:
    with pytest.raises(ValueError):
        ForkPolicy.last_n(0)


def test_agent_message_marks_control_messages() -> None:
    assert AgentMessage(source=None, content={}, sequence=1).is_control
    assert not AgentMessage(source="root", content="hi", sequence=2).is_control


def test_agent_spec_holds_runner_codec_role() -> None:
    spec = AgentSpec(runner=EchoRunner(), codec=JsonCodec(), role="debater")
    assert spec.role == "debater"


def test_error_code_values_match_spec() -> None:
    assert [c.value for c in ErrorCode] == [
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "BUSY",
        "CLOSED",
        "LIMIT_REACHED",
        "BUDGET_EXHAUSTED",
        "INVALID_REQUEST",
        "CODEC_ERROR",
        "STORE_UNAVAILABLE",
        "INTERNAL",
    ]


def test_error_hierarchy_matches_spec() -> None:
    from athena.core.agent_kernel.types import (
        AgentBusyError,
        AgentCommandError,
        AgentError,
        AgentRunFailed,
        AgentRunInterrupted,
    )

    assert issubclass(AgentCommandError, AgentError)
    assert issubclass(AgentBusyError, AgentCommandError)
    assert issubclass(AgentRunFailed, AgentError)
    assert issubclass(AgentRunInterrupted, AgentError)
    assert AgentBusyError().code == ErrorCode.BUSY

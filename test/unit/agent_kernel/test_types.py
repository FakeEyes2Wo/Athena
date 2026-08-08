import pytest

from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentMessage,
    AgentSpec,
    AgentStatus,
    ErrorCode,
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
        "waiting",
        "waiting_for_human",
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


def test_agent_message_marks_control_messages() -> None:
    assert AgentMessage(source=None, content="control").is_control
    assert not AgentMessage(source="root", content="hi").is_control


def test_agent_message_carries_context_refs() -> None:
    msg = AgentMessage(source="root", content="评审意见", context_refs=["ref://r"])
    assert msg.context_refs == ["ref://r"]


def test_agent_spec_holds_runner_and_codec() -> None:
    spec = AgentSpec(runner=EchoRunner(), codec=JsonCodec())
    assert isinstance(spec.runner, EchoRunner)
    assert isinstance(spec.codec, JsonCodec)


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

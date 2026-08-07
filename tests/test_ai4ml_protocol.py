import re

import pytest
from pydantic import ValidationError

from athena.core.contracts import (
    ErrorRecord,
    EventEnvelope,
    ExperimentId,
    HypothesisId,
    RunId,
    new_id,
)
from athena.core.thread_models import AgentTask


def test_new_id_uses_prefix_and_short_hex_suffix() -> None:
    assert re.fullmatch(r"run_[0-9a-f]{12}", new_id("run"))


def test_new_id_rejects_empty_prefix() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "prefix must contain at least one alphanumeric character and may otherwise "
            "contain only alphanumeric characters or underscores"
        ),
    ):
        new_id("")


def test_event_envelope_round_trips_through_json() -> None:
    envelope = EventEnvelope(
        kind="experiment.started",
        source="orchestrator",
        payload={"run_id": "run_abc123"},
        state_version=0,
    )

    assert EventEnvelope.model_validate_json(envelope.model_dump_json()) == envelope


def test_event_envelope_rejects_negative_state_version() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(
            kind="experiment.started",
            source="orchestrator",
            payload={"run_id": "run_abc123"},
            state_version=-1,
        )


def test_error_record_round_trips_through_json_and_rejects_negative_retries() -> None:
    error = ErrorRecord(
        severity="retry",
        code="TEMPORARY_FAILURE",
        message="retry later",
        retry_count=1,
    )

    assert ErrorRecord.model_validate_json(error.model_dump_json()) == error
    with pytest.raises(ValidationError):
        ErrorRecord(
            severity="retry",
            code="TEMPORARY_FAILURE",
            message="retry later",
            retry_count=-1,
        )


def test_error_record_rejects_invalid_severity() -> None:
    with pytest.raises(ValidationError):
        ErrorRecord(
            severity="warning",
            code="TEMPORARY_FAILURE",
            message="retry later",
        )


def test_agent_task_context_refs_are_not_shared() -> None:
    first = AgentTask(task_id="task_1", agent_type="planner", command="plan")
    second = AgentTask(task_id="task_2", agent_type="planner", command="plan")

    first.context_refs.append("artifact://context")

    assert second.context_refs == []


def test_protocol_identifiers_are_newtype_string_wrappers() -> None:
    run_id = RunId("run_123")
    hypothesis_id = HypothesisId("hypothesis_123")
    experiment_id = ExperimentId("experiment_123")

    assert isinstance(run_id, str)
    assert isinstance(hypothesis_id, str)
    assert isinstance(experiment_id, str)

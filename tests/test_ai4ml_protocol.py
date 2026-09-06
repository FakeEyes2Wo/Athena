import re

import pytest
from pydantic import ValidationError

from athena.core.contracts import (
    ErrorRecord,
    EventEnvelope,
    new_id,
)


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

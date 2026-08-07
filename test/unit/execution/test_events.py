from datetime import datetime, timezone

import pytest

from athena.execution import ExecutionEvent, ExecutionEventKind, MonitorLimits


def test_monitor_limits_require_positive_finite_durations() -> None:
    with pytest.raises(ValueError, match="stalled_after"):
        MonitorLimits(stalled_after=0, timeout_after=10)
    with pytest.raises(ValueError, match="timeout_after"):
        MonitorLimits(stalled_after=1, timeout_after=-1)
    with pytest.raises(ValueError, match="stalled_after"):
        MonitorLimits(stalled_after=float("inf"), timeout_after=10)


def test_execution_event_requires_non_blank_id() -> None:
    with pytest.raises(ValueError, match="execution_id"):
        ExecutionEvent(" ", ExecutionEventKind.STARTED)


def test_execution_event_requires_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="UTC"):
        ExecutionEvent(
            "turn:1",
            ExecutionEventKind.ACTIVITY,
            occurred_at=datetime.now(),
        )


def test_execution_event_retains_explicit_progress_and_metadata() -> None:
    now = datetime.now(timezone.utc)
    event = ExecutionEvent(
        "turn:1",
        ExecutionEventKind.ACTIVITY,
        occurred_at=now,
        advances_progress=True,
        metadata={"native_kind": "agent/text_delta"},
    )

    assert event.occurred_at is now
    assert event.advances_progress is True
    assert event.metadata == {"native_kind": "agent/text_delta"}
    with pytest.raises(TypeError):
        event.metadata["native_kind"] = "changed"


def test_execution_event_rejects_non_json_metadata() -> None:
    with pytest.raises(TypeError, match="JSON-safe"):
        ExecutionEvent(
            "turn:1",
            ExecutionEventKind.ACTIVITY,
            metadata={"bad": object()},
        )

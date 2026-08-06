import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from athena.app_server.events import EventJournal
from athena.app_server.observability import (
    AGENT_PROGRESS_EVENTS,
    ThreadExecutionObserver,
)
from athena.execution import ExecutionState, MonitorLimits


class FakeClock:
    def __init__(self) -> None:
        self.monotonic = 0.0
        self.utc = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.utc += timedelta(seconds=seconds)


def make_observer(clock: FakeClock, journal: EventJournal) -> ThreadExecutionObserver:
    return ThreadExecutionObserver(
        journal,
        limits=MonitorLimits(stalled_after=5, timeout_after=20),
        scan_interval=0.01,
        terminal_retention=30,
        clock=lambda: clock.monotonic,
        utc_now=lambda: clock.utc,
    )


async def test_agent_adapter_only_advances_explicit_progress_events() -> None:
    clock = FakeClock()
    observer = make_observer(clock, EventJournal("thread:1"))
    await observer.turn_started("turn:1")
    started_progress = observer.monitor.snapshot("turn:1").last_progress_at

    clock.advance(1)
    await observer.agent_event("turn:1", "log", "event:1", None)
    assert observer.monitor.snapshot("turn:1").last_progress_at == started_progress

    clock.advance(1)
    await observer.agent_event("turn:1", "agent/text_delta", "event:2", {"delta": "x"})

    assert observer.monitor.snapshot("turn:1").last_progress_at == clock.utc
    assert AGENT_PROGRESS_EVENTS == {
        "agent/text_delta",
        "tool/begin",
        "tool/end",
        "tool/error",
    }


@pytest.mark.parametrize("kind", sorted(AGENT_PROGRESS_EVENTS))
async def test_each_agent_progress_kind_resets_progress(kind: str) -> None:
    clock = FakeClock()
    observer = make_observer(clock, EventJournal("thread:1"))
    await observer.turn_started("turn:1")
    clock.advance(1)

    await observer.agent_event("turn:1", kind, "event:1", None)

    assert observer.monitor.snapshot("turn:1").last_progress_at == clock.utc


async def test_health_changes_are_written_to_journal_once() -> None:
    clock = FakeClock()
    journal = EventJournal("thread:1")
    observer = make_observer(clock, journal)

    await observer.turn_started("turn:1")
    await observer.agent_event("turn:1", "heartbeat", "event:1", None)
    await observer.turn_completed("turn:1")
    await observer.turn_completed("turn:1")

    assert [record.kind for record in journal._records] == [
        "execution/health",
        "execution/health",
    ]
    assert [record.data["state"] for record in journal._records] == [
        ExecutionState.RUNNING.value,
        ExecutionState.COMPLETED.value,
    ]
    assert all(record.turn_id == "turn:1" for record in journal._records)


async def test_cancelled_turn_is_removed_without_false_failure() -> None:
    clock = FakeClock()
    journal = EventJournal("thread:1")
    observer = make_observer(clock, journal)
    await observer.turn_started("turn:1")

    await observer.turn_cancelled("turn:1")

    assert observer.monitor.snapshot("turn:1") is None
    assert [record.data["state"] for record in journal._records] == ["RUNNING"]


async def test_observer_owns_and_stops_periodic_scan_task() -> None:
    clock = FakeClock()
    observer = make_observer(clock, EventJournal("thread:1"))

    await observer.start()
    task = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "execution-monitor-thread:1"
    )
    await observer.stop()

    assert task.done()

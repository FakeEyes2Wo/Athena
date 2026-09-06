import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from athena.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionState,
    HealthStateEvent,
    MonitorLimits,
)
from athena.execution.monitor import ExecutionMonitor


class FakeClock:
    def __init__(self) -> None:
        self.monotonic = 0.0
        self.utc = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.utc += timedelta(seconds=seconds)


def event(
    clock: FakeClock,
    kind: ExecutionEventKind,
    *,
    execution_id: str = "turn:1",
    advances_progress: bool = False,
) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id,
        kind,
        occurred_at=clock.utc,
        advances_progress=advances_progress,
    )


def make_monitor(
    clock: FakeClock,
    emitted: list[HealthStateEvent],
    *,
    terminal_retention: float = 30,
) -> ExecutionMonitor:
    async def collect(health: HealthStateEvent) -> None:
        emitted.append(health)

    return ExecutionMonitor(
        collect,
        scan_interval=0.01,
        terminal_retention=terminal_retention,
        clock=lambda: clock.monotonic,
        utc_now=lambda: clock.utc,
    )


async def test_stalled_execution_only_recovers_on_explicit_progress() -> None:
    clock = FakeClock()
    emitted: list[HealthStateEvent] = []
    monitor = make_monitor(clock, emitted)
    limits = MonitorLimits(stalled_after=5, timeout_after=20)

    await monitor.observe(event(clock, ExecutionEventKind.STARTED), limits=limits)
    clock.advance(5)
    await monitor.sweep()
    stalled_progress_at = monitor.snapshot("turn:1").last_progress_at

    clock.advance(1)
    await monitor.observe(event(clock, ExecutionEventKind.ACTIVITY))
    assert monitor.snapshot("turn:1").state is ExecutionState.STALLED
    assert monitor.snapshot("turn:1").last_progress_at == stalled_progress_at

    clock.advance(1)
    await monitor.observe(
        event(clock, ExecutionEventKind.ACTIVITY, advances_progress=True)
    )

    assert [item.state for item in emitted] == [
        ExecutionState.RUNNING,
        ExecutionState.STALLED,
        ExecutionState.RUNNING,
    ]
    assert monitor.snapshot("turn:1").last_progress_at == clock.utc


async def test_timeout_takes_precedence_and_allows_late_completion() -> None:
    clock = FakeClock()
    emitted: list[HealthStateEvent] = []
    monitor = make_monitor(clock, emitted)
    limits = MonitorLimits(stalled_after=5, timeout_after=10)

    await monitor.observe(event(clock, ExecutionEventKind.STARTED), limits=limits)
    clock.advance(10)
    await monitor.sweep()
    await monitor.observe(event(clock, ExecutionEventKind.COMPLETED))

    assert [item.state for item in emitted] == [
        ExecutionState.RUNNING,
        ExecutionState.TIMEOUT,
        ExecutionState.COMPLETED,
    ]
    snapshot = monitor.snapshot("turn:1")
    assert snapshot.state is ExecutionState.COMPLETED
    assert snapshot.timeout_at is not None
    assert snapshot.completed_at == clock.utc


async def test_late_terminal_event_evaluates_timeout_before_completion() -> None:
    clock = FakeClock()
    emitted: list[HealthStateEvent] = []
    monitor = make_monitor(clock, emitted)
    limits = MonitorLimits(stalled_after=5, timeout_after=10)

    await monitor.observe(event(clock, ExecutionEventKind.STARTED), limits=limits)
    clock.advance(11)
    await monitor.observe(event(clock, ExecutionEventKind.COMPLETED))

    assert [item.state for item in emitted] == [
        ExecutionState.RUNNING,
        ExecutionState.TIMEOUT,
        ExecutionState.COMPLETED,
    ]


async def test_failed_event_is_final_and_deduplicated() -> None:
    clock = FakeClock()
    emitted: list[HealthStateEvent] = []
    monitor = make_monitor(clock, emitted)

    await monitor.observe(event(clock, ExecutionEventKind.STARTED))
    await monitor.observe(event(clock, ExecutionEventKind.FAILED))
    await monitor.observe(event(clock, ExecutionEventKind.FAILED))
    await monitor.observe(
        event(clock, ExecutionEventKind.ACTIVITY, advances_progress=True)
    )

    assert [item.state for item in emitted] == [
        ExecutionState.RUNNING,
        ExecutionState.FAILED,
    ]
    assert monitor.snapshot("turn:1").failed_at == clock.utc


async def test_duplicate_start_is_idempotent_but_rejects_conflicting_limits() -> None:
    clock = FakeClock()
    emitted: list[HealthStateEvent] = []
    monitor = make_monitor(clock, emitted)
    limits = MonitorLimits(stalled_after=5, timeout_after=10)
    started = event(clock, ExecutionEventKind.STARTED)

    first = await monitor.observe(started, limits=limits)
    second = await monitor.observe(started, limits=limits)

    assert first == second
    assert [item.state for item in emitted] == [ExecutionState.RUNNING]
    with pytest.raises(ValueError, match="conflicting limits"):
        await monitor.observe(
            started,
            limits=MonitorLimits(stalled_after=6, timeout_after=10),
        )


async def test_unknown_execution_is_rejected() -> None:
    clock = FakeClock()
    monitor = make_monitor(clock, [])

    with pytest.raises(KeyError, match="unknown execution"):
        await monitor.observe(event(clock, ExecutionEventKind.ACTIVITY))


async def test_sink_failure_does_not_abort_monitoring() -> None:
    clock = FakeClock()
    calls = 0

    async def failing_sink(_health: HealthStateEvent) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("journal unavailable")

    monitor = ExecutionMonitor(
        failing_sink,
        clock=lambda: clock.monotonic,
        utc_now=lambda: clock.utc,
    )

    await monitor.observe(event(clock, ExecutionEventKind.STARTED))
    await monitor.observe(event(clock, ExecutionEventKind.COMPLETED))

    assert calls == 2
    assert monitor.snapshot("turn:1").state is ExecutionState.COMPLETED


async def test_explicit_removal_and_terminal_retention_pruning() -> None:
    clock = FakeClock()
    monitor = make_monitor(clock, [], terminal_retention=5)

    await monitor.observe(event(clock, ExecutionEventKind.STARTED))
    await monitor.observe(event(clock, ExecutionEventKind.COMPLETED))
    assert monitor.remove("missing") is False
    assert monitor.remove("turn:1") is True
    assert monitor.snapshot("turn:1") is None

    await monitor.observe(
        event(clock, ExecutionEventKind.STARTED, execution_id="turn:2")
    )
    await monitor.observe(
        event(clock, ExecutionEventKind.COMPLETED, execution_id="turn:2")
    )
    clock.advance(5)
    await monitor.sweep()

    assert monitor.snapshot("turn:2") is None


async def test_run_stops_when_owner_sets_stop_event() -> None:
    clock = FakeClock()
    monitor = make_monitor(clock, [])
    stop = asyncio.Event()

    task = asyncio.create_task(monitor.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=0.1)

    assert task.done()


async def test_remove_is_safe_while_health_sink_is_waiting() -> None:
    clock = FakeClock()
    sink_started = asyncio.Event()
    release_sink = asyncio.Event()

    async def blocking_sink(health: HealthStateEvent) -> None:
        if health.state is ExecutionState.STALLED:
            sink_started.set()
            await release_sink.wait()

    monitor = ExecutionMonitor(
        blocking_sink,
        clock=lambda: clock.monotonic,
        utc_now=lambda: clock.utc,
    )
    limits = MonitorLimits(stalled_after=1, timeout_after=10)
    await monitor.observe(event(clock, ExecutionEventKind.STARTED), limits=limits)
    await monitor.observe(
        event(clock, ExecutionEventKind.STARTED, execution_id="turn:2"),
        limits=limits,
    )
    clock.advance(1)
    sweeping = asyncio.create_task(monitor.sweep())
    await sink_started.wait()

    assert monitor.remove("turn:2") is True
    release_sink.set()
    await sweeping

    assert monitor.snapshot("turn:2") is None


def test_monitor_timing_configuration_must_be_finite() -> None:
    async def sink(_health: HealthStateEvent) -> None:
        return None

    with pytest.raises(ValueError, match="scan_interval"):
        ExecutionMonitor(sink, scan_interval=float("inf"))
    with pytest.raises(ValueError, match="terminal_retention"):
        ExecutionMonitor(sink, terminal_retention=float("nan"))

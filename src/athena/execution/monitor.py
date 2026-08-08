"""Passive state tracking for long-running executions."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from athena.execution.events import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionSnapshot,
    ExecutionState,
    HealthEventSink,
    HealthStateEvent,
    MonitorLimits,
    require_positive_finite,
    utc_now,
)

logger = logging.getLogger(__name__)

_FINAL_STATES = frozenset({ExecutionState.COMPLETED, ExecutionState.FAILED})


@dataclass(slots=True)
class _ExecutionRecord:
    execution_id: str
    state: ExecutionState
    limits: MonitorLimits
    started_mono: float
    last_event_mono: float
    last_progress_mono: float
    started_at: datetime
    last_event_at: datetime
    last_progress_at: datetime
    stalled_at: datetime | None = None
    timeout_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    final_mono: float | None = None


class ExecutionMonitor:
    """Observe events and emit derived health states without controlling work."""

    def __init__(
        self,
        sink: HealthEventSink,
        *,
        default_limits: MonitorLimits | None = None,
        scan_interval: float = 1.0,
        terminal_retention: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = utc_now,
    ) -> None:
        require_positive_finite(scan_interval, "scan_interval")
        require_positive_finite(terminal_retention, "terminal_retention")
        self._sink = sink
        self._default_limits = default_limits or MonitorLimits()
        self._scan_interval = scan_interval
        self._terminal_retention = terminal_retention
        self._clock = clock
        self._utc_now = utc_now
        self._records: dict[str, _ExecutionRecord] = {}
        self._lock = asyncio.Lock()
        self._sink_lock = asyncio.Lock()

    async def observe(
        self,
        event: ExecutionEvent,
        *,
        limits: MonitorLimits | None = None,
    ) -> ExecutionSnapshot:
        """Apply one event in arrival order and return the latest snapshot."""
        notifications: list[HealthStateEvent] = []
        async with self._lock:
            if event.kind is ExecutionEventKind.STARTED:
                snapshot, started = self._start(event, limits or self._default_limits)
                if started is not None:
                    notifications.append(started)
            else:
                if limits is not None:
                    raise ValueError("limits may only be supplied with a started event")

                record = self._records.get(event.execution_id)
                if record is None:
                    logger.warning(
                        "event for unknown execution: %s", event.execution_id
                    )
                    raise KeyError(f"unknown execution: {event.execution_id}")
                if record.state in _FINAL_STATES:
                    logger.warning(
                        "ignoring %s event after final state for %s",
                        event.kind.value,
                        event.execution_id,
                    )
                    return self._snapshot(record)

                now_mono = self._clock()
                now_utc = self._utc_now()
                notifications.extend(self._evaluate_due(record, now_mono, now_utc))
                record.last_event_mono = now_mono
                record.last_event_at = now_utc

                if event.kind is ExecutionEventKind.ACTIVITY:
                    if event.advances_progress:
                        record.last_progress_mono = now_mono
                        record.last_progress_at = now_utc
                        if record.state is ExecutionState.STALLED:
                            transition = self._transition(
                                record,
                                ExecutionState.RUNNING,
                                now_mono,
                                now_utc,
                            )
                            if transition is not None:
                                notifications.append(transition)
                elif event.kind is ExecutionEventKind.COMPLETED:
                    transition = self._transition(
                        record, ExecutionState.COMPLETED, now_mono, now_utc
                    )
                    if transition is not None:
                        notifications.append(transition)
                elif event.kind is ExecutionEventKind.FAILED:
                    transition = self._transition(
                        record, ExecutionState.FAILED, now_mono, now_utc
                    )
                    if transition is not None:
                        notifications.append(transition)

                snapshot = self._snapshot(record)

        await self._emit_all(notifications)
        return snapshot

    async def sweep(self) -> tuple[ExecutionSnapshot, ...]:
        """Evaluate timers and prune final records whose retention has elapsed."""
        notifications: list[HealthStateEvent] = []
        async with self._lock:
            now_mono = self._clock()
            now_utc = self._utc_now()
            expired: list[str] = []
            for execution_id, record in self._records.items():
                if record.state in _FINAL_STATES:
                    if (
                        record.final_mono is not None
                        and now_mono - record.final_mono >= self._terminal_retention
                    ):
                        expired.append(execution_id)
                    continue
                notifications.extend(self._evaluate_due(record, now_mono, now_utc))
            for execution_id in expired:
                del self._records[execution_id]
            snapshots = tuple(
                self._snapshot(record) for record in self._records.values()
            )
        await self._emit_all(notifications)
        return snapshots

    async def run(self, stop: asyncio.Event) -> None:
        """Scan until the app-server owner requests shutdown."""
        while not stop.is_set():
            await self.sweep()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._scan_interval)
            except TimeoutError:
                continue

    def snapshot(self, execution_id: str) -> ExecutionSnapshot | None:
        """Return the current snapshot for one execution, if retained."""
        record = self._records.get(execution_id)
        return self._snapshot(record) if record is not None else None

    def snapshots(self) -> tuple[ExecutionSnapshot, ...]:
        """Return all retained execution snapshots in registration order."""
        return tuple(self._snapshot(record) for record in self._records.values())

    def remove(self, execution_id: str) -> bool:
        """Forget an execution explicitly, returning whether it existed."""
        return self._records.pop(execution_id, None) is not None

    def _start(
        self, event: ExecutionEvent, limits: MonitorLimits
    ) -> tuple[ExecutionSnapshot, HealthStateEvent | None]:
        existing = self._records.get(event.execution_id)
        if existing is not None:
            if existing.limits != limits:
                logger.warning("conflicting limits for %s", event.execution_id)
                raise ValueError(
                    f"conflicting limits for execution: {event.execution_id}"
                )
            return self._snapshot(existing), None

        now_mono = self._clock()
        now_utc = self._utc_now()
        record = _ExecutionRecord(
            execution_id=event.execution_id,
            state=ExecutionState.RUNNING,
            limits=limits,
            started_mono=now_mono,
            last_event_mono=now_mono,
            last_progress_mono=now_mono,
            started_at=now_utc,
            last_event_at=now_utc,
            last_progress_at=now_utc,
        )
        self._records[event.execution_id] = record
        health = self._health_event(record, now_utc)
        return health.snapshot, health

    def _evaluate_due(
        self, record: _ExecutionRecord, now_mono: float, now_utc: datetime
    ) -> list[HealthStateEvent]:
        if record.state in _FINAL_STATES or record.state is ExecutionState.TIMEOUT:
            return []
        if now_mono - record.started_mono >= record.limits.timeout_after:
            event = self._transition(record, ExecutionState.TIMEOUT, now_mono, now_utc)
            return [event] if event is not None else []
        if (
            record.state is ExecutionState.RUNNING
            and now_mono - record.last_progress_mono >= record.limits.stalled_after
        ):
            event = self._transition(record, ExecutionState.STALLED, now_mono, now_utc)
            return [event] if event is not None else []
        return []

    def _transition(
        self,
        record: _ExecutionRecord,
        state: ExecutionState,
        now_mono: float,
        now_utc: datetime,
    ) -> HealthStateEvent | None:
        if record.state is state:
            return None
        record.state = state
        if state is ExecutionState.STALLED:
            record.stalled_at = now_utc
        elif state is ExecutionState.TIMEOUT:
            record.timeout_at = now_utc
        elif state is ExecutionState.COMPLETED:
            record.completed_at = now_utc
            record.final_mono = now_mono
        elif state is ExecutionState.FAILED:
            record.failed_at = now_utc
            record.final_mono = now_mono
        return self._health_event(record, now_utc)

    async def _emit_all(self, events: list[HealthStateEvent]) -> None:
        async with self._sink_lock:
            for event in events:
                try:
                    await self._sink(event)
                except Exception:
                    logger.exception(
                        "health sink failed for %s in state %s",
                        event.execution_id,
                        event.state.value,
                    )

    def _health_event(
        self, record: _ExecutionRecord, changed_at: datetime
    ) -> HealthStateEvent:
        return HealthStateEvent(
            execution_id=record.execution_id,
            state=record.state,
            changed_at=changed_at,
            snapshot=self._snapshot(record),
        )

    @staticmethod
    def _snapshot(record: _ExecutionRecord) -> ExecutionSnapshot:
        return ExecutionSnapshot(
            execution_id=record.execution_id,
            state=record.state,
            started_at=record.started_at,
            last_event_at=record.last_event_at,
            last_progress_at=record.last_progress_at,
            limits=record.limits,
            stalled_at=record.stalled_at,
            timeout_at=record.timeout_at,
            completed_at=record.completed_at,
            failed_at=record.failed_at,
        )

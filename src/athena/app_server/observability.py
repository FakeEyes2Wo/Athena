"""App-server adaptation for source-independent execution observability."""

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from athena.app_server.events import Event, EventJournal
from athena.core.tool_types import TOOL_BEGIN, TOOL_END, TOOL_ERROR
from athena.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionMonitor,
    HealthStateEvent,
    MonitorLimits,
)
from athena.execution.events import utc_now as execution_utc_now

AGENT_PROGRESS_EVENTS = frozenset(
    {"agent/text_delta", TOOL_BEGIN, TOOL_END, TOOL_ERROR}
)


class ThreadExecutionObserver:
    """Map one app-server Thread's turn events into monitor observations."""

    def __init__(
        self,
        journal: EventJournal,
        *,
        limits: MonitorLimits | None = None,
        scan_interval: float = 1.0,
        terminal_retention: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = execution_utc_now,
    ) -> None:
        self._journal = journal
        self._limits = limits or MonitorLimits()
        self._utc_now = utc_now
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.monitor = ExecutionMonitor(
            self._record_health,
            default_limits=self._limits,
            scan_interval=scan_interval,
            terminal_retention=terminal_retention,
            clock=clock,
            utc_now=utc_now,
        )

    async def start(self) -> None:
        """Start the app-server-owned periodic monitor scan."""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self.monitor.run(self._stop),
            name=f"execution-monitor-{self._journal.thread_id}",
        )

    async def stop(self) -> None:
        """Stop the periodic scan without changing any execution."""
        task = self._task
        if task is None:
            return
        self._stop.set()
        await task
        self._task = None

    async def turn_started(self, turn_id: str) -> None:
        """Register a running turn."""
        await self.monitor.observe(
            self._event(turn_id, ExecutionEventKind.STARTED),
            limits=self._limits,
        )

    async def agent_event(
        self,
        turn_id: str,
        kind: str,
        event_ref: str,
        data: dict | None,
    ) -> None:
        """Observe a native Agent event with an explicit progress mapping."""
        del data
        await self.monitor.observe(
            self._event(
                turn_id,
                ExecutionEventKind.ACTIVITY,
                advances_progress=kind in AGENT_PROGRESS_EVENTS,
                metadata={"native_kind": kind, "event_ref": event_ref},
            )
        )

    async def turn_completed(self, turn_id: str) -> None:
        """Record successful completion."""
        await self.monitor.observe(self._event(turn_id, ExecutionEventKind.COMPLETED))

    async def turn_failed(self, turn_id: str, exception_type: str) -> None:
        """Record failed completion without consuming the original exception."""
        await self.monitor.observe(
            self._event(
                turn_id,
                ExecutionEventKind.FAILED,
                metadata={"exception_type": exception_type},
            )
        )

    async def turn_cancelled(self, turn_id: str) -> None:
        """Forget an interrupted turn without misclassifying it as failed."""
        self.monitor.remove(turn_id)

    def _event(
        self,
        turn_id: str,
        kind: ExecutionEventKind,
        *,
        advances_progress: bool = False,
        metadata: dict | None = None,
    ) -> ExecutionEvent:
        return ExecutionEvent(
            execution_id=turn_id,
            kind=kind,
            occurred_at=self._utc_now(),
            advances_progress=advances_progress,
            metadata=metadata or {},
        )

    async def _record_health(self, health: HealthStateEvent) -> None:
        # A future Handler may subscribe to terminal and unhealthy states here.
        async with self._journal.condition:
            self._journal.append(
                Event(
                    thread_id=self._journal.thread_id,
                    turn_id=health.execution_id,
                    sequence=self._journal.next_sequence(),
                    kind="execution/health",
                    event_ref=f"execution-health:{uuid4().hex}",
                    data={
                        "execution_id": health.execution_id,
                        "state": health.state.value,
                        "changed_at": health.changed_at.isoformat(),
                    },
                )
            )

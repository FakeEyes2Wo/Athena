import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from athena.app_server.events import Event, EventJournal

SHORT_DEADLINE = 0.1


async def append_current_event(
    journal: EventJournal,
    *,
    turn_id: str | None = None,
    kind: str = "item",
    event_ref: str = "artifact:event",
) -> Event:
    """Append an event through EventJournal's current lock-owning call pattern."""

    async with journal.condition:
        event = Event(
            thread_id=journal.thread_id,
            turn_id=turn_id,
            sequence=journal.next_sequence(),
            kind=kind,
            event_ref=event_ref,
        )
        journal.append(event)
        return event


async def eventually(
    predicate: Callable[[], bool],
    *,
    timeout: float = 0.5,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before deadline")
        await asyncio.sleep(0)


async def cancel_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    live = [task for task in tasks if task is not None and not task.done()]
    for task in live:
        task.cancel()
    if live:
        await asyncio.gather(*live, return_exceptions=True)


async def break_self_wait_cycles(*tasks: asyncio.Task[Any]) -> None:
    """Unwind the legacy shutdown task that awaits a gather containing itself."""

    for task in tasks:
        waiter = getattr(task, "_fut_waiter", None)
        children = getattr(waiter, "_children", ())
        if waiter is not None and not waiter.done() and task in children:
            waiter.set_exception(asyncio.CancelledError())
    await asyncio.sleep(0)


async def cancel_event_waiters() -> None:
    waiters = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and getattr(task.get_coro(), "__qualname__", "") == "Event.wait"
    ]
    await cancel_tasks(*waiters)


async def force_stop_processor(processor: Any) -> None:
    """Test-only cleanup for a processor stuck in a failed shutdown path."""

    tasks = list(processor._inflight.values())
    dispatcher = processor._dispatcher_task
    await break_self_wait_cycles(*tasks)
    await cancel_tasks(*tasks, dispatcher)
    processor._inflight.clear()


async def immediate_runner(thread, turn, emit) -> tuple[str, str]:
    del thread, turn
    await emit("item", "artifact:item")
    return "artifact:result", "artifact:next-context"


class BlockingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.entries = 0

    async def __call__(self, thread, turn, emit) -> tuple[str, str]:
        del thread, turn, emit
        self.entries += 1
        self.started.set()
        await self.release.wait()
        return "artifact:result", "artifact:next-context"


async def call_with_cleanup(
    awaitable: Awaitable[Any], cleanup: Callable[[], Awaitable[None]]
) -> Any:
    try:
        return await awaitable
    finally:
        await cleanup()

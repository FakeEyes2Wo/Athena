"""Test AgentMonitor timeout and success detection."""

import asyncio
import pytest
from athena.code.monitor import AgentMonitor, WatchResult


@pytest.mark.asyncio
async def test_watch_ok():
    """watch() returns OK when coroutine completes in time."""
    monitor = AgentMonitor()

    async def quick():
        return "done"

    result = await monitor.watch(quick(), timeout_s=5)
    assert result.status == "OK"
    assert result.result == "done"


@pytest.mark.asyncio
async def test_watch_timeout():
    """watch() returns TIMEOUT when coroutine exceeds limit."""
    monitor = AgentMonitor()

    async def slow():
        await asyncio.sleep(10)
        return "never"

    result = await monitor.watch(slow(), timeout_s=0.1)
    assert result.status == "TIMEOUT"
    assert result.result is None
    assert "0.1" in result.error or "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_watch_timeout_cancels_and_awaits_operation(monkeypatch):
    """Timeout owns operation cleanup before returning to the caller."""
    monitor = AgentMonitor()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def operation():
        try:
            started.set()
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned_up.set()

    async def timeout_without_owning_task(awaitable, timeout):
        await asyncio.sleep(0)
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", timeout_without_owning_task)

    result = await monitor.watch(operation(), timeout_s=1)

    assert result.status == "TIMEOUT"
    assert started.is_set()
    assert cleaned_up.is_set()


@pytest.mark.asyncio
async def test_watch_fail():
    """watch() returns FAIL when coroutine raises."""
    monitor = AgentMonitor()

    async def bad():
        raise ValueError("boom")

    result = await monitor.watch(bad(), timeout_s=5)
    assert result.status == "FAIL"
    assert "boom" in result.error


def test_monitor_signals_stall_on_tenth_turn_without_progress():
    """The configured ten-turn boundary signals a stalled agent."""
    monitor = AgentMonitor(stall_threshold=10)

    assert monitor.is_stalled(9) is False
    assert monitor.is_stalled(10) is True

"""AgentMonitor: timeout + stall detection for agent execution."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal

@dataclass
class WatchResult:
    """Outcome of a monitored agent execution with status and error info."""
    status: Literal["OK", "TIMEOUT", "FAIL"]
    result: Any = None
    error: str = ""

class AgentMonitor:
    """Watch agent execution for timeout. Stalls logged as warnings."""
    def __init__(self, stall_threshold: int = 10):
        self._stall_threshold = stall_threshold

    def is_stalled(self, turns_without_progress: int) -> bool:
        return turns_without_progress >= self._stall_threshold

    async def watch(
        self,
        operation: Awaitable[Any],
        timeout_s: float = 600,
    ) -> WatchResult:
        """Execute coroutine with timeout, returning status dict with result or error."""
        task = asyncio.ensure_future(operation)
        try:
            result = await asyncio.wait_for(task, timeout=timeout_s)
            return WatchResult(status="OK", result=result)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return WatchResult(
                status="TIMEOUT",
                error=f"timed out after {timeout_s}s",
            )
        except Exception as exc:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return WatchResult(
                status="FAIL",
                error=str(exc),
            )

if __name__ == "__main__":
    async def _demo():
        async def _fast():
            return "done"
        monitor = AgentMonitor()
        result = await monitor.watch(_fast(), timeout_s=5)
        print(f"Fast task result: {result}")

        async def _slow():
            await asyncio.sleep(10)
            return "never"
        result = await monitor.watch(_slow(), timeout_s=1)
        print(f"Slow task result: {result}")

    asyncio.run(_demo())

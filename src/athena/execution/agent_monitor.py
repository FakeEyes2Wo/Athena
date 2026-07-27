"""Watch CodeAgent execution for timeout and stalls."""

import asyncio


class AgentMonitor:
    """Watch CodeAgent execution for timeout and stalls."""

    async def watch(self, coro, timeout_s: int = 600) -> dict:
        try:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
            return {"status": "OK", "result": result}
        except asyncio.TimeoutError:
            return {
                "status": "TIMEOUT",
                "result": None,
                "error": f"Exceeded {timeout_s}s",
            }
        except Exception as e:
            return {"status": "FAIL", "result": None, "error": str(e)}

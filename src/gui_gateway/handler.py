"""Thin GUI protocol adapter for the shared research runtime."""

from athena.research import ResearchRuntime


class GuiRequestHandler:
    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "ping":
            return {"pong": True}
        return await self._runtime.dispatch(method, params)

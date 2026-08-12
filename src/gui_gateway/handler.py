"""Thin GUI adapter for the public research runtime."""

from typing import Any

from athena.research import ResearchRuntime


class GuiRequestHandler:
    """Expose only runtime lifecycle, text input, and exact controls."""

    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, object]:
        if method == "ping":
            return {"pong": True}
        if method == "start":
            await self._runtime.start()
            return {"started": True}
        if method == "message":
            text = params.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("message text must be a non-empty string")
            return {"response": await self._runtime.message(text)}
        if method in {"pause", "resume", "stop"}:
            return {"status": await self._runtime.message(f"/{method}")}
        raise ValueError(f"unsupported GUI method: {method}")

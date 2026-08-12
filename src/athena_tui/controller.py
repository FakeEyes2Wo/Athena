"""Thin adapter from ResearchRuntime's two-event protocol to the TUI."""

from collections.abc import Callable

from athena.research.supervisor.events import OutputEvent, StateEvent

TuiEvent = OutputEvent | StateEvent
EmitFn = Callable[[TuiEvent], None]


class TuiController:
    """Subscribe once, validate runtime records, and forward text messages."""

    def __init__(self, runtime: object, *, emit: EmitFn) -> None:
        self._runtime = runtime
        self._emit = emit
        self._subscription_id: str | None = None
        self.events_seen: list[str] = []
        self._subscribe()

    def _subscribe(self) -> None:
        if self._subscription_id is None:
            self._subscription_id = self._runtime.subscribe(self._on_runtime_event)

    async def connect(self) -> None:
        """Idempotently attach to the runtime event stream."""
        self._subscribe()

    def _on_runtime_event(self, kind: str, payload: dict) -> None:
        if kind == "output":
            event: TuiEvent = OutputEvent.model_validate(payload)
        elif kind == "state":
            event = StateEvent.model_validate(payload)
        else:
            raise ValueError(f"unsupported runtime event kind: {kind}")
        self.events_seen.append(kind)
        self._emit(event)

    async def send_message(self, text: str) -> str:
        """Forward one composer message to the runtime command surface."""
        return await self._runtime.message(text)

    async def run(self) -> None:
        """Ensure the controller is connected without starting a poll loop."""
        await self.connect()

    async def aclose(self) -> None:
        """Unsubscribe and close the owned runtime."""
        if self._subscription_id is not None:
            self._runtime.unsubscribe(self._subscription_id)
            self._subscription_id = None
        await self._runtime.aclose()

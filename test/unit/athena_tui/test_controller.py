"""TUI controller tests for the output/state-only runtime protocol."""

from pathlib import Path

import pytest

from athena.research.supervisor.events import OutputEvent, StateEvent
from athena_tui.controller import TuiController


def full_state(*, status: str = "RUNNING") -> dict[str, object]:
    return {
        "type": "state",
        "status": status,
        "phase": "SEARCH",
        "plans": [],
        "search": {"attempts": 0, "limit": 10, "successes": 0, "concurrency": 4},
        "sota": None,
        "waiting": None,
    }


class FakeRuntime:
    def __init__(self) -> None:
        self.subscribers: dict[str, object] = {}
        self.messages: list[str] = []
        self.closed = False

    def subscribe(self, callback) -> str:
        self.subscribers["sub-1"] = callback
        callback("state", full_state())
        return "sub-1"

    def unsubscribe(self, subscription_id: str) -> None:
        self.subscribers.pop(subscription_id, None)

    async def message(self, text: str) -> str:
        self.messages.append(text)
        return "accepted"

    async def aclose(self) -> None:
        self.closed = True

    def emit(self, kind: str, payload: dict[str, object]) -> None:
        for callback in tuple(self.subscribers.values()):
            callback(kind, payload)


@pytest.mark.asyncio
async def test_controller_consumes_only_output_and_state(monkeypatch) -> None:
    runtime = FakeRuntime()
    events: list[object] = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("TUI must not read runtime/session files")

    monkeypatch.setattr(Path, "read_text", forbidden)
    controller = TuiController(runtime, emit=events.append)
    await controller.connect()
    runtime.emit(
        "output",
        {
            "type": "output",
            "seq": 1,
            "source": "agent",
            "channel": "text",
            "text": "hello",
            "plan": None,
            "tool": None,
            "artifact_ref": None,
            "truncated": False,
        },
    )

    assert [event.type for event in events] == ["state", "output"]
    assert isinstance(events[0], StateEvent)
    assert isinstance(events[1], OutputEvent)
    assert controller.events_seen == ["state", "output"]


@pytest.mark.asyncio
async def test_controller_uses_message_as_its_only_command_surface() -> None:
    runtime = FakeRuntime()
    controller = TuiController(runtime, emit=lambda _event: None)
    await controller.connect()

    assert await controller.send_message("try ViT next") == "accepted"
    assert await controller.send_message("/pause") == "accepted"
    assert runtime.messages == ["try ViT next", "/pause"]


@pytest.mark.asyncio
async def test_controller_rejects_unknown_runtime_event_kind() -> None:
    runtime = FakeRuntime()
    controller = TuiController(runtime, emit=lambda _event: None)
    await controller.connect()

    with pytest.raises(ValueError, match="unsupported runtime event"):
        runtime.emit("status", {"type": "status"})


@pytest.mark.asyncio
async def test_close_unsubscribes_and_closes_runtime() -> None:
    runtime = FakeRuntime()
    controller = TuiController(runtime, emit=lambda _event: None)
    await controller.connect()

    await controller.aclose()

    assert runtime.subscribers == {}
    assert runtime.closed is True

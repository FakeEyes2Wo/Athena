from pathlib import Path

import pytest

from gui_gateway.handler import GuiRequestHandler


class RecordingRuntime:
    def __init__(self) -> None:
        self.started = False
        self.messages: list[str] = []
        self.tree_path = Path("/tmp/athena-runtime")

    async def start(self) -> None:
        self.started = True

    async def message(self, text: str) -> str:
        self.messages.append(text)
        return "accepted"


@pytest.mark.asyncio
async def test_handler_exposes_only_start_and_message() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)

    assert await handler.dispatch("ping", {}) == {"pong": True}
    assert await handler.dispatch("start", {}) == {"started": True}
    assert await handler.dispatch("message", {"text": "try trees"}) == {
        "response": "accepted"
    }
    assert runtime.started is True
    assert runtime.messages == ["try trees"]


@pytest.mark.asyncio
async def test_handler_maps_exact_controls_to_messages() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)

    for method in ("pause", "resume", "stop"):
        assert await handler.dispatch(method, {}) == {"status": "accepted"}

    assert runtime.messages == ["/pause", "/resume", "/stop"]


@pytest.mark.asyncio
async def test_handler_rejects_unknown_methods() -> None:
    handler = GuiRequestHandler(RecordingRuntime())
    with pytest.raises(ValueError, match="unsupported GUI method"):
        await handler.dispatch("STATUS", {})
    with pytest.raises(ValueError, match="message text"):
        await handler.dispatch("message", {})

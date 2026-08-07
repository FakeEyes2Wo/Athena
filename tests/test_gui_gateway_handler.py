import pytest

from gui_gateway.handler import GuiRequestHandler


class RecordingRuntime:
    def __init__(self) -> None:
        self.calls = []

    async def dispatch(self, method, params):
        self.calls.append((method, params))
        return {"forwarded": method}


@pytest.mark.asyncio
async def test_handler_keeps_ping_local() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)
    assert await handler.dispatch("ping", {}) == {"pong": True}
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_handler_forwards_method_and_params_exactly() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)
    params = {"path": "tree.json"}
    assert await handler.dispatch("tree_load", params) == {"forwarded": "tree_load"}
    assert runtime.calls == [("tree_load", params)]


def test_handler_owns_no_research_state() -> None:
    handler = GuiRequestHandler(RecordingRuntime())
    for name in (
        "_tree",
        "_budget",
        "_active_task",
        "_active_loop",
        "_search_status",
        "_validate",
        "_reporter",
        "_tree_add_node",
    ):
        assert not hasattr(handler, name)

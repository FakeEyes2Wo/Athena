"""gui_gateway.__main__ entrypoint tests: runtime factory options."""

from pathlib import Path

import pytest


def test_make_runtime_enables_auto_validate_and_accepts_skip_validate(
    monkeypatch,
) -> None:
    """GUI runtime must auto-enter VALIDATE after SEARCH (mirrors TUI/CLI)."""
    from gui_gateway import __main__ as main

    seen: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, **options) -> None:
            seen.update(options)
            self.tree_path = Path(options["project_root"]) / ".athena" / "tree.json"

        def settings(self):
            return {"project_root": seen["project_root"]}

    monkeypatch.setattr(main, "ResearchRuntime", FakeRuntime)
    main._make_runtime("/tmp/proj", skip_validate=True)

    assert seen.get("auto_validate") is True
    assert seen.get("skip_validate") is True
    assert seen.get("project_root") == "/tmp/proj"
    assert seen.get("task_confirmation_gate") is True
    assert seen.get("auto_confirm") is False


@pytest.mark.asyncio
async def test_start_server_factory_restores_project_skip_validate(
    monkeypatch, tmp_path
):
    from gui_gateway import __main__ as main
    from gui_gateway.state_store import GuiState

    project = tmp_path / "project"
    project.mkdir()
    store = main.GuiStateStore(tmp_path / "gui-state.json")
    store.save(
        GuiState(
            active_project_root=str(project),
            skip_validate_by_project={str(project.resolve()): True},
        )
    )
    seen: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, **options) -> None:
            seen.update(options)
            self.tree_path = Path(options["project_root"]) / ".athena" / "tree.json"

        def settings(self):
            return {"project_root": seen["project_root"]}

    class FakeServer:
        sockets = [type("Socket", (), {"getsockname": lambda self: (None, 17601)})()]

    class FakeTransport:
        def __init__(self, handler) -> None:
            self.handler = handler

        async def serve(self, host, port):
            return FakeServer()

    monkeypatch.setattr(main, "ResearchRuntime", FakeRuntime)
    monkeypatch.setattr(main, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(main, "GuiStateStore", lambda: store)

    server, port = await main.start_server(test_mode=True, port=17601)

    assert server is not None
    assert port == 17601
    assert seen["project_root"] == str(project)
    assert seen["skip_validate"] is True

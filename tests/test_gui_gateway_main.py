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

    research = seen["research"]
    assert research.policy.auto_validate is True
    assert research.policy.skip_validate is True
    assert seen.get("project_root") == "/tmp/proj"
    assert research.task.confirmation_gate is True
    assert research.task.auto_confirm is False


def test_controller_capabilities_are_bound_per_project_and_session(
    monkeypatch, tmp_path
):
    from gui_gateway import __main__ as main
    from gui_gateway.controller import ControllerCapabilities

    captured = []
    bindings = []
    monkeypatch.setattr(
        main, "ResearchRuntime", lambda **options: captured.append(options)
    )

    async def restart_service():
        pass

    def factory(root, session_id):
        authority = object()
        bindings.append((root, session_id, authority))
        return ControllerCapabilities(authority, {"restart_service": restart_service})

    for project, session_id in (("first", "default"), ("second", "conversation-2")):
        main._make_runtime(
            str(tmp_path / project), session_id=session_id, controller_factory=factory
        )

    assert [(root, session) for root, session, _ in bindings] == [
        ((tmp_path / "first").resolve(), "default"),
        ((tmp_path / "second").resolve(), "conversation-2"),
    ]
    for options, (_, _, authority) in zip(captured, bindings, strict=True):
        deps = options["dependencies"]
        assert deps.baseline_authority is authority
        assert deps.environment_repair_actions == {"restart_service": restart_service}


def test_unconfigured_gui_does_not_construct_local_authority(monkeypatch):
    from gui_gateway import __main__ as main

    captured = {}
    monkeypatch.setattr(
        main, "ResearchRuntime", lambda **options: captured.update(options)
    )
    main._make_runtime()
    assert captured["dependencies"].baseline_authority is None
    assert captured["dependencies"].environment_repair_actions is None


def test_controller_factory_is_loaded_only_from_explicit_host_import(monkeypatch):
    from gui_gateway import __main__ as main

    calls = []
    sentinel = lambda *_args: None
    monkeypatch.setenv("ATHENA_CONTROLLER_FACTORY", "host.controller:build")
    monkeypatch.setattr(
        main.importlib,
        "import_module",
        lambda module: calls.append(module) or type("Module", (), {"build": sentinel}),
    )
    assert main._controller_factory_from_environment() is sentinel
    assert calls == ["host.controller"]


@pytest.mark.parametrize("value", ["host.controller", ":build", "host.controller:"])
def test_controller_factory_rejects_malformed_host_reference(monkeypatch, value):
    from gui_gateway import __main__ as main

    monkeypatch.setenv("ATHENA_CONTROLLER_FACTORY", value)
    with pytest.raises(RuntimeError, match="package.module:callable"):
        main._controller_factory_from_environment()


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

    def capture_factory(project_root, state_root, **options):
        del state_root
        seen["factory_project_root"] = project_root
        return FakeRuntime(project_root=project_root, **options)

    class FakeServer:
        def __init__(self) -> None:
            self.sockets = [
                type("Socket", (), {"getsockname": lambda self: (None, 17601)})()
            ]

    class FakeTransport:
        def __init__(self, handler) -> None:
            self.handler = handler

        async def serve(self, host, port):
            return FakeServer()

    monkeypatch.setattr(main, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(main, "GuiStateStore", lambda: store)

    controller_factory = lambda root, session_id: None
    server, port = await main.start_server(
        test_mode=True,
        make_runtime=capture_factory,
        port=17601,
        controller_factory=controller_factory,
    )

    assert server is not None
    assert port == 17601
    assert seen["factory_project_root"] == str(project)
    assert seen["skip_validate"] is True
    assert seen["controller_factory"] is controller_factory


@pytest.mark.asyncio
async def test_start_server_keeps_legacy_runtime_factory_compatible(
    monkeypatch, tmp_path
):
    from gui_gateway import __main__ as main

    class Runtime:
        def settings(self):
            return {"project_root": str(tmp_path)}

    def legacy_factory(
        project_root,
        state_root,
        ask_user=None,
        session_id="default",
        broker=None,
        skip_validate=False,
    ):
        del project_root, state_root, ask_user, session_id, broker, skip_validate
        return Runtime()

    class Server:
        def __init__(self):
            self.sockets = [
                type("Socket", (), {"getsockname": lambda _self: (None, 17601)})()
            ]

    class Transport:
        def __init__(self, _handler):
            pass

        async def serve(self, _host, _port):
            return Server()

    monkeypatch.setattr(main, "WebSocketTransport", Transport)
    await main.start_server(
        test_mode=True,
        make_runtime=legacy_factory,
        controller_factory=lambda _root, _session: None,
        port=17601,
    )

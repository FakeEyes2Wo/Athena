"""gui_gateway.__main__ entrypoint tests: runtime factory options."""


def test_make_runtime_enables_auto_validate(monkeypatch) -> None:
    """GUI runtime must auto-enter VALIDATE after SEARCH (mirrors TUI/CLI)."""
    from gui_gateway import __main__ as main

    seen: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, **options) -> None:
            seen.update(options)

    monkeypatch.setattr(main, "ResearchRuntime", FakeRuntime)
    main._make_runtime("/tmp/proj")

    assert seen.get("auto_validate") is True
    assert seen.get("project_root") == "/tmp/proj"
    assert seen.get("task_confirmation_gate") is True
    assert seen.get("auto_confirm") is False

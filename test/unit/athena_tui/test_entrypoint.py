"""athena_tui entrypoint 测试：参数解析、TTY 检测、非 TTY 提示。"""

import pytest

from athena_tui import entrypoint


def test_parse_default_project() -> None:
    args = entrypoint._parse([])
    assert args.project == ".athena/tui-run"


def test_parse_custom_project() -> None:
    args = entrypoint._parse(["--project", "/tmp/p"])
    assert args.project == "/tmp/p"


def test_is_interactive_false_when_not_tty(monkeypatch) -> None:
    class NotTTY:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(entrypoint.sys, "stdin", NotTTY())
    monkeypatch.setattr(entrypoint.sys, "stdout", NotTTY())
    assert entrypoint._is_interactive() is False


def test_is_interactive_true_when_both_tty(monkeypatch) -> None:
    class TTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(entrypoint.sys, "stdin", TTY())
    monkeypatch.setattr(entrypoint.sys, "stdout", TTY())
    assert entrypoint._is_interactive() is True


def test_main_non_tty_exits_with_automation_hint(monkeypatch, capsys) -> None:
    class NotTTY:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(entrypoint.sys, "stdin", NotTTY())
    monkeypatch.setattr(entrypoint.sys, "stdout", NotTTY())
    assert entrypoint.main([]) == 1
    assert "Athena-cli" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_does_not_auto_start_and_enables_task_seeding(monkeypatch) -> None:
    """_run 不自动 start：首条 Human 消息经 auto_seed_task 从 PREPARE 启动。"""
    calls: list[str] = []
    seen: dict[str, object] = {}

    class Runtime:
        def __init__(self, **options) -> None:
            seen.update(options)

    class App:
        def __init__(self, runtime, _project) -> None:
            assert isinstance(runtime, Runtime)

        async def run(self) -> int:
            calls.append("app")
            return 7

    monkeypatch.setattr(entrypoint, "ResearchRuntime", Runtime)
    monkeypatch.setattr(entrypoint, "AthenaApp", App)
    args = entrypoint._parse(["--project", "p"])

    assert await entrypoint._run(args) == 7
    assert calls == ["app"]
    assert seen.get("auto_seed_task") is True

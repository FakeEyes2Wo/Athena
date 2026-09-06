"""athena_tui entrypoint 测试：参数解析、TTY 检测、非 TTY 提示。"""

import pytest

from athena_tui import entrypoint


def test_parse_default_project() -> None:
    args = entrypoint._parse([])
    assert args.project == ".athena/tui-run"
    assert args.search_limit == 10
    assert args.validate is False


def test_parse_explicit_search_and_validation_options() -> None:
    args = entrypoint._parse(["--search-limit", "3", "--validate"])
    assert args.search_limit == 3
    assert args.validate is True


def test_parse_rejects_non_positive_search_limit() -> None:
    with pytest.raises(SystemExit):
        entrypoint._parse(["--search-limit", "0"])


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
    error = capsys.readouterr().err
    assert "Athena TUI 需要交互式终端" in error
    assert "自动化请使用 Athena-cli" in error


@pytest.mark.asyncio
async def test_run_does_not_auto_start_and_enables_task_seeding(monkeypatch) -> None:
    """无 SOTA 时不自动 start：首条 Human 消息经 auto_seed_task 从 PREPARE 启动。"""
    calls: list[str] = []
    seen: dict[str, object] = {}

    class _Tree:
        def best_experiment_id(self):
            return None

    class Runtime:
        def __init__(self, **options) -> None:
            seen.update(options)
            self.tree = _Tree()

        async def start(self) -> None:
            calls.append("start")

    class App:
        def __init__(self, runtime, _project) -> None:
            assert isinstance(runtime, Runtime)

        async def run(self) -> int:
            calls.append("app")
            return 7

    monkeypatch.setattr(entrypoint, "ResearchRuntime", Runtime)
    monkeypatch.setattr(entrypoint, "AthenaApp", App)
    args = entrypoint._parse(["--project", "p", "--search-limit", "3"])

    assert await entrypoint._run(args) == 7
    assert calls == ["app"]
    research = seen["research"]
    assert research.task.auto_seed is True
    assert research.search.search_limit == 3
    assert research.policy.auto_validate is False


@pytest.mark.asyncio
async def test_run_auto_starts_when_sota_exists(monkeypatch) -> None:
    """已有 SOTA 时自动 start 续跑（断点续传）。"""
    calls: list[str] = []

    class _Tree:
        def best_experiment_id(self):
            return "exp_baseline"

    class Runtime:
        def __init__(self, **options) -> None:
            self.tree = _Tree()

        async def start(self) -> None:
            calls.append("start")

    class App:
        def __init__(self, runtime, _project) -> None:
            pass

        async def run(self) -> int:
            calls.append("app")
            return 0

    monkeypatch.setattr(entrypoint, "ResearchRuntime", Runtime)
    monkeypatch.setattr(entrypoint, "AthenaApp", App)
    args = entrypoint._parse(["--project", "p"])

    assert await entrypoint._run(args) == 0
    assert calls == ["start", "app"]

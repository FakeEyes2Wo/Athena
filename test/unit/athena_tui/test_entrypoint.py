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
    args = entrypoint._parse(["--project", "p"])

    assert await entrypoint._run(args) == 7
    assert calls == ["app"]
    assert seen.get("auto_seed_task") is True
    assert seen.get("auto_validate") is True


def test_parse_defaults_leave_the_platform_split_off() -> None:
    """No dataset arguments means the evaluator splits, as before this existed."""
    args = entrypoint._parse([])

    assert args.data is None
    assert args.target is None
    assert args.group_column is None
    assert args.split_seed == 0
    assert args.data_root is None
    assert args.direction == "maximize"
    assert args.tolerance == 0.0
    assert args.survey is False
    # 这条分支原先把 auto_validate 写死为 True；加开关不改默认行为。
    assert args.validate is True


def test_validate_can_be_switched_off() -> None:
    assert entrypoint._parse(["--no-validate"]).validate is False


def test_dataset_options_forward_the_contract_for_a_local_csv(tmp_path) -> None:
    csv = tmp_path / "model_input.csv"
    csv.write_text("image_filename,TIC,label\na,1,0\n", encoding="utf-8")
    args = entrypoint._parse(
        [
            "--data", str(csv),
            "--target", "label",
            "--group-column", "TIC",
            "--split-seed", "62",
            "--data-root", str(tmp_path),
            "--tolerance", "0.005",
            "--experiment-timeout", "3600",
            "--survey",
        ]
    )

    options = entrypoint._dataset_options(args)

    assert options["dataset_path"] == csv.resolve()
    assert options["target_column"] == "label"
    assert options["group_column"] == "TIC"
    assert options["split_seed"] == 62
    assert options["data_root"] == str(tmp_path)
    assert options["tolerance"] == 0.005
    assert options["experiment_timeout_s"] == 3600
    assert options["survey"] is True


def test_a_group_column_is_not_forwarded_when_the_platform_does_not_split(
    tmp_path,
) -> None:
    """Otherwise the caller believes grouping is on while rows shuffle freely."""
    args = entrypoint._parse(
        ["--data", str(tmp_path / "images"), "--target", "label", "--group-column", "AR"]
    )

    options = entrypoint._dataset_options(args)

    assert options["dataset_path"] is None
    assert options["target_column"] is None
    assert options["group_column"] is None


def test_the_startup_line_names_the_split_or_says_it_is_off(tmp_path) -> None:
    csv = tmp_path / "d.csv"
    csv.write_text("a,label\n1,0\n", encoding="utf-8")

    on = entrypoint._describe_split(
        entrypoint._dataset_options(
            entrypoint._parse(
                ["--data", str(csv), "--target", "label", "--group-column", "TIC",
                 "--split-seed", "62"]
            )
        )
    )
    assert "开启" in on and "TIC" in on and "62" in on

    off = entrypoint._describe_split(entrypoint._dataset_options(entrypoint._parse([])))
    assert "关闭" in off

    ungrouped = entrypoint._describe_split(
        entrypoint._dataset_options(
            entrypoint._parse(["--data", str(csv), "--target", "label"])
        )
    )
    assert "行级随机划分" in ungrouped


@pytest.mark.asyncio
async def test_run_hands_the_dataset_contract_to_the_runtime(monkeypatch, tmp_path):
    """The whole point: without these the platform never splits by group."""
    seen: dict[str, object] = {}

    class _Tree:
        def best_experiment_id(self):
            return None

    class Runtime:
        def __init__(self, **options) -> None:
            seen.update(options)
            self.tree = _Tree()

    class App:
        def __init__(self, runtime, _project) -> None:
            pass

        async def run(self) -> int:
            return 0

    csv = tmp_path / "model_input.csv"
    csv.write_text("image_filename,TIC,label\na,1,0\n", encoding="utf-8")
    monkeypatch.setattr(entrypoint, "ResearchRuntime", Runtime)
    monkeypatch.setattr(entrypoint, "AthenaApp", App)
    args = entrypoint._parse(
        [
            "--project", str(tmp_path / "p"),
            "--data", str(csv),
            "--target", "label",
            "--group-column", "TIC",
            "--split-seed", "62",
            "--data-root", str(tmp_path),
            "--tolerance", "0.005",
            "--search-limit", "10",
        ]
    )

    assert await entrypoint._run(args) == 0

    assert seen["dataset_path"] == csv.resolve()
    assert seen["target_column"] == "label"
    assert seen["group_column"] == "TIC"
    assert seen["split_seed"] == 62
    assert seen["data_root"] == str(tmp_path)
    assert seen["tolerance"] == 0.005
    assert seen["search_limit"] == 10
    assert seen["auto_seed_task"] is True


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

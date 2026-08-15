"""Tests for the output/state-only Athena CLI."""

import json

import pytest

import athena.cli as cli


def _state(*, status: str = "RUNNING", phase: str = "SEARCH") -> dict[str, object]:
    return {
        "type": "state",
        "status": status,
        "phase": phase,
        "plans": [],
        "search": {"attempts": 0, "limit": 3, "successes": 0, "concurrency": 4},
        "sota": None,
        "waiting": None,
    }


class FakeRuntime:
    def __init__(self) -> None:
        self.callback = None
        self.calls: list[object] = []
        self.closed = False

    def subscribe(self, callback) -> str:
        self.calls.append("subscribe")
        self.callback = callback
        callback("state", _state(phase="PREPARE"))
        return "sub-1"

    def unsubscribe(self, subscription_id: str) -> None:
        self.calls.append(("unsubscribe", subscription_id))
        self.callback = None

    async def start(self) -> None:
        self.calls.append("start")
        self.callback(
            "output",
            {
                "type": "output",
                "seq": 1,
                "source": "agent",
                "channel": "text",
                "text": "working",
                "plan": None,
                "tool": None,
                "artifact_ref": None,
                "truncated": False,
            },
        )
        self.callback("state", _state(status="COMPLETED", phase="COMPLETED"))

    async def message(self, text: str) -> str:
        self.calls.append(("message", text))
        return {"/pause": "WAITING", "/resume": "RUNNING", "/stop": "STOPPED"}[text]

    async def aclose(self) -> None:
        self.closed = True


def test_parser_exposes_only_current_runtime_commands() -> None:
    parser = cli._build_parser()
    for command in ("run", "status", "pause", "resume", "stop"):
        argv = [command, "--project", "p"]
        if command == "run":
            argv += ["--data", "d"]
        assert parser.parse_args(argv).command == command

    for removed in ("retry", "requests", "reply"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed, "--project", "p"])


def test_run_options_configure_runtime_constructor() -> None:
    args = cli._build_parser().parse_args(
        [
            "run",
            "--project",
            "p",
            "--data",
            "dataset/train.csv",
            "--task",
            "predict churn",
            "--target",
            "churned",
            "--direction",
            "minimize",
            "--mode",
            "auto",
            "--max-search-experiments",
            "3",
        ]
    )

    assert cli._runtime_options(args) == {
        "task": "predict churn\nDataset path: dataset/train.csv\nTarget: churned",
        "search_limit": 3,
        "auto_validate": True,
        "direction": "minimize",
        # 消融开关默认走门禁；--ideation baseline 是对照组。
        "ideation": "gated",
    }


def test_ideation_ablation_flag_reaches_the_runtime_constructor() -> None:
    """--ideation baseline 必须真的传到 runtime，否则消融开关是摆设。"""
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", "d.csv", "--ideation", "baseline"]
    )
    assert cli._runtime_options(args)["ideation"] == "baseline"


@pytest.mark.asyncio
async def test_run_subscribes_before_start_and_waits_for_terminal_state(
    monkeypatch, capsys
) -> None:
    runtime = FakeRuntime()
    captured: dict[str, object] = {}

    def build(project: str, **options: object) -> FakeRuntime:
        captured.update({"project": project, **options})
        return runtime

    monkeypatch.setattr(cli, "_runtime", build)
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", "d", "--task", "task"]
    )

    assert await cli._cmd_run(args) == 0
    assert runtime.calls[:2] == ["subscribe", "start"]
    assert runtime.closed is True
    assert captured["task"] == "task\nDataset path: d"
    output = capsys.readouterr().out
    assert "agent> working" in output
    assert "phase=COMPLETED status=COMPLETED" in output


@pytest.mark.asyncio
async def test_status_prints_one_complete_state_snapshot(monkeypatch, capsys) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(cli, "_runtime", lambda _project: runtime)
    args = cli._build_parser().parse_args(["status", "--project", "p"])

    assert await cli._cmd_status(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == _state(phase="PREPARE")
    assert runtime.calls == ["subscribe", ("unsubscribe", "sub-1")]
    assert runtime.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "message", "expected"),
    [
        ("pause", "/pause", "WAITING"),
        ("resume", "/resume", "RUNNING"),
        ("stop", "/stop", "STOPPED"),
    ],
)
async def test_controls_use_the_single_message_surface(
    command, message, expected, monkeypatch, capsys
) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(cli, "_runtime", lambda _project: runtime)
    args = cli._build_parser().parse_args([command, "--project", "p"])

    assert await cli._dispatch_command(args) == 0
    assert runtime.calls == [("message", message)]
    assert capsys.readouterr().out.strip() == f"status={expected}"
    assert runtime.closed is True


def test_main_accepts_explicit_argv(monkeypatch) -> None:
    async def fake_dispatch(args):
        assert args.command == "status"
        return 17

    monkeypatch.setattr(cli, "_dispatch_command", fake_dispatch)
    assert cli.main(["status", "--project", "p"]) == 17

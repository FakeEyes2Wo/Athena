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
        # 消融开关默认走 idea generation 门禁；--ideation baseline 是对照组。
        "ideation": "ideageneration",
        "survey": False,
        "survey_query": "",
        "survey_max_papers": 10,
    }


def test_the_literature_survey_stays_off_unless_it_is_asked_for() -> None:
    """默认不跑：一次调研是十几分钟的模型往返，不能由默认值替用户决定花这笔钱。"""
    base = ["run", "--project", "p", "--data", "d.csv"]
    parser = cli._build_parser()

    default = cli._runtime_options(parser.parse_args(base))
    enabled = cli._runtime_options(
        parser.parse_args([*base, "--survey", "--survey-papers", "4"])
    )

    assert default["survey"] is False
    assert enabled["survey"] is True
    assert enabled["survey_max_papers"] == 4


def test_run_defaults_to_auto_validate_for_headless_cli() -> None:
    args = cli._build_parser().parse_args(["run", "--project", "p", "--data", "d"])
    assert args.mode == "auto"
    assert cli._runtime_options(args)["auto_validate"] is True


def test_run_preserves_zero_search_limit() -> None:
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", "d", "--max-search-experiments", "0"]
    )
    assert cli._runtime_options(args)["search_limit"] == 0


def test_run_rejects_negative_search_limit() -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["run", "--project", "p", "--data", "d", "--max-search-experiments", "-1"]
        )


@pytest.mark.asyncio
async def test_run_times_out_when_workflow_hangs(monkeypatch, capsys, tmp_path) -> None:
    class HangingRuntime(FakeRuntime):
        async def start(self) -> None:
            self.calls.append("start")
            # 不发布终态事件，模拟永远跑不完的工作流

    data = tmp_path / "train.csv"
    data.write_text("x,y\n1,0\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_runtime", lambda _project, **_options: HangingRuntime())
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", str(data), "--timeout", "1"]
    )
    assert await cli._cmd_run(args) == 1
    assert "timed out" in capsys.readouterr().err


def test_ideation_ablation_flag_reaches_the_runtime_constructor() -> None:
    """--ideation baseline 必须真的传到 runtime，否则消融开关是摆设。"""
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", "d.csv", "--ideation", "baseline"]
    )
    assert cli._runtime_options(args)["ideation"] == "baseline"


def test_ideation_debate_flag_reaches_the_runtime_constructor() -> None:
    """--ideation debate 必须真的传到 runtime，否则辩论式 Ideator 无法被选择。"""
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", "d.csv", "--ideation", "debate"]
    )
    assert cli._runtime_options(args)["ideation"] == "debate"


@pytest.mark.asyncio
async def test_run_subscribes_before_start_and_waits_for_terminal_state(
    monkeypatch, capsys, tmp_path
) -> None:
    runtime = FakeRuntime()
    captured: dict[str, object] = {}
    data = tmp_path / "train.csv"
    data.write_text("x,y\n1,0\n", encoding="utf-8")

    def build(project: str, **options: object) -> FakeRuntime:
        captured.update({"project": project, **options})
        return runtime

    monkeypatch.setattr(cli, "_runtime", build)
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", str(data), "--task", "task"]
    )

    assert await cli._cmd_run(args) == 0
    assert runtime.calls[:2] == ["subscribe", "start"]
    assert runtime.closed is True
    assert captured["task"] == f"task\nDataset path: {data}"
    output = capsys.readouterr().out
    assert "agent> working" in output
    assert "phase=COMPLETED status=COMPLETED" in output


def test_event_renderer_coalesces_agent_fragments(capsys) -> None:
    renderer = cli._EventRenderer()
    renderer.render(
        "output", {"source": "agent", "channel": "text", "text": "hel", "plan": "p"}
    )
    renderer.render(
        "output", {"source": "agent", "channel": "text", "text": "lo", "plan": "p"}
    )
    renderer.render("state", {"phase": "SEARCH", "status": "RUNNING"})
    assert capsys.readouterr().out == "agent> hello\nphase=SEARCH status=RUNNING\n"


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


class _UnprintableRuntime(FakeRuntime):
    """Agent 输出里带一个终端编码装不下的字符，随后立刻报 FAILED。

    ``_emit`` 复刻 ``runtime_events.invoke``：订阅者抛异常时只记一笔日志、不往上抛。
    这正是真实运行里"进程既没崩也没退出"的机制——没有这层吞异常，测试里异常会直接
    冒泡成另一条退出路径，反而测不到卡死。
    """

    def __init__(self) -> None:
        super().__init__()
        self.subscriber_errors = 0

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self.callback(kind, payload)
        except Exception:  # noqa: BLE001 - 与事件总线一致：订阅者故障不影响运行时
            self.subscriber_errors += 1

    async def start(self) -> None:
        self.calls.append("start")
        self._emit(
            "output",
            {
                "type": "output",
                "seq": 1,
                "source": "agent",
                "channel": "text",
                "text": "done ✅",
                "plan": None,
                "tool": None,
                "artifact_ref": None,
                "truncated": False,
            },
        )
        self._emit("state", _state(status="FAILED", phase="SEARCH"))


@pytest.mark.asyncio
async def test_a_run_that_fails_still_exits_when_its_output_cannot_be_printed(
    monkeypatch, capsys, tmp_path
) -> None:
    """真实跑测（2026-08-16）：一个 ✅ 让 FAILED 的运行挂到 --timeout 才退出。

    ``print`` 在 GBK 控制台上抛 ``UnicodeEncodeError``，异常打断了 ``receive``，
    ``terminal.set()`` 因此从没执行。渲染失败只能少一行日志，不能改变退出行为。
    """
    runtime = _UnprintableRuntime()
    monkeypatch.setattr(cli, "_runtime", lambda project, **options: runtime)

    class _Gbk:
        encoding = "gbk"

        def write(self, text: str) -> int:
            text.encode("gbk")  # 复现真实终端：装不下就抛
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(cli.sys, "stdout", _Gbk())
    data = tmp_path / "train.csv"
    data.write_text("x,y\n1,0\n", encoding="utf-8")
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", str(data), "--timeout", "5"]
    )

    assert await cli._cmd_run(args) == 1
    assert runtime.closed is True
    # 关键断言：退出码 1 必须来自 FAILED 状态，而不是 --timeout 兜底——两条路都返回
    # 1，只断言退出码的话这个用例在修复前也会"通过"。
    assert "timed out" not in capsys.readouterr().err

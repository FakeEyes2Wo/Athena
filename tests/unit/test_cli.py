"""Tests for the output/state-only Athena CLI."""

import json

import pytest

from athena import cli
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S


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


def test_parser_exposes_runtime_control_commands_including_continue_alias() -> None:
    parser = cli._build_parser()
    for command in ("run", "status", "pause", "resume", "continue", "stop"):
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

    research, dependencies = cli._runtime_options(args)
    assert research.task.text == (
        "predict churn\nDataset path: dataset/train.csv\nTarget: churned"
    )
    assert research.search.search_limit == 3
    assert research.policy.auto_validate is True
    assert research.policy.direction == "minimize"
    # 消融开关默认走 idea generation 门禁；--ideation baseline 是对照组。
    assert research.policy.ideation == "ideageneration"
    assert research.survey.enabled is False
    assert research.survey.query == ""
    assert research.survey.max_papers == 20
    assert research.survey.search_top_k == 0
    assert research.survey.max_seconds == 0.0
    assert dependencies.execution.experiment_timeout_s == DEFAULT_EXPERIMENT_TIMEOUT_S
    assert dependencies.execution.compute is not None


def test_the_data_contract_reaches_the_runtime_not_just_the_prompt(tmp_path) -> None:
    """``--target``/``--tolerance`` 之类此前只被拼进提示词文本，从没传给运行时。

    后果不是"少一个参数"：``prepare_phase`` 要求 ``dataset_path`` 与
    ``target_column`` 同时非空才做平台数据划分，两者永远是 None，于是那段代码
    一次都没执行过——划分始终由评估器 Agent 自己做。
    """
    source = tmp_path / "windows.csv"
    source.write_text("TIC,feature,label\n1,2,0\n1,3,1\n2,4,0\n", encoding="utf-8")

    research, _dependencies = cli._runtime_options(
        cli._build_parser().parse_args(
            [
                "run",
                "--project",
                "p",
                "--data",
                str(source),
                "--target",
                "label",
                "--group-column",
                "TIC",
                "--split-seed",
                "62",
                "--tolerance",
                "0.005",
            ]
        )
    )

    assert research.dataset.path == source
    assert research.dataset.target_column == "label"
    assert research.dataset.group_column == "TIC"
    assert research.dataset.split_seed == 62
    assert research.policy.tolerance == 0.005
    assert "TIC" in research.task.text


def test_the_platform_split_stays_off_for_inputs_it_cannot_split(tmp_path) -> None:
    """``--data`` 可以是目录或 Kaggle URL；对它们调 materialize_csv_split 会直接炸。"""
    directory = tmp_path / "images"
    directory.mkdir()

    for argv in (
        ["run", "--project", "p", "--data", str(directory), "--target", "label"],
        ["run", "--project", "p", "--data", "https://kaggle.com/c/titanic"],
    ):
        research, _dependencies = cli._runtime_options(
            cli._build_parser().parse_args(argv)
        )
        assert research.dataset.path is None
        assert research.dataset.target_column is None


def test_the_dataset_path_in_the_prompt_is_absolute(tmp_path, monkeypatch) -> None:
    """Agents run from their own workspace, so a relative --data path misleads them.

    真机（2026-08-29）：``--data ../data/.../model_input.csv`` 通过了 CLI 的存在性
    预检，然后第一个 agent 把整轮预算花在找这个文件上——从项目根解析
    ``../data/...`` 得到的是项目的兄弟目录，不是 CLI 的。
    """
    source = tmp_path / "windows.csv"
    source.write_text("TIC,feature,label\n1,2,0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    research, _dependencies = cli._runtime_options(
        cli._build_parser().parse_args(
            ["run", "--project", "p", "--data", "windows.csv", "--target", "label"]
        )
    )

    assert f"Dataset path: {source.resolve()}" in research.task.text
    assert research.dataset.path == source.resolve()


def test_a_non_path_dataset_argument_is_left_alone(tmp_path, monkeypatch) -> None:
    """``--data`` 也可以是 Kaggle URL；不存在的东西不该被当路径展开。"""
    monkeypatch.chdir(tmp_path)
    url = "https://www.kaggle.com/competitions/titanic"

    research, _dependencies = cli._runtime_options(
        cli._build_parser().parse_args(["run", "--project", "p", "--data", url])
    )

    assert f"Dataset path: {url}" in research.task.text


def test_tolerance_rejects_negative_values() -> None:
    """``PlanInput.tolerance`` 声明了 ge=0；负值该在解析期就失败，而不是烧掉一轮 PREPARE。"""
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["run", "--project", "p", "--data", "d.csv", "--tolerance", "-0.1"]
        )


def test_run_defaults_to_auto_validate_for_headless_cli() -> None:
    args = cli._build_parser().parse_args(["run", "--project", "p", "--data", "d"])
    assert args.mode == "auto"
    research, _dependencies = cli._runtime_options(args)
    assert research.policy.auto_validate is True


def test_run_preserves_zero_search_limit() -> None:
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", "d", "--max-search-experiments", "0"]
    )
    research, _dependencies = cli._runtime_options(args)
    assert research.search.search_limit == 0


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
    research, _dependencies = cli._runtime_options(args)
    assert research.policy.ideation == "baseline"


def test_ideation_debate_flag_reaches_the_runtime_constructor() -> None:
    """--ideation debate 必须真的传到 runtime，否则辩论式 Ideator 无法被选择。"""
    args = cli._build_parser().parse_args(
        ["run", "--project", "p", "--data", "d.csv", "--ideation", "debate"]
    )
    research, _dependencies = cli._runtime_options(args)
    assert research.policy.ideation == "debate"


def test_the_literature_survey_stays_off_unless_it_is_asked_for() -> None:
    """默认不跑：一次调研要十几分钟的模型调用，不能由默认值替用户决定花这笔钱。"""
    base = ["run", "--project", "p", "--data", "d.csv"]
    parser = cli._build_parser()

    default, _dependencies = cli._runtime_options(parser.parse_args(base))
    enabled, _dependencies = cli._runtime_options(
        parser.parse_args([*base, "--survey", "--survey-papers", "4"])
    )

    assert default.survey.enabled is False
    assert enabled.survey.enabled is True
    assert enabled.survey.max_papers == 4


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
    assert captured["research"].task.text == f"task\nDataset path: {data}"
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


@pytest.mark.asyncio
async def test_continue_cli_alias_uses_resume_control(monkeypatch, capsys) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(cli, "_runtime", lambda _project: runtime)
    args = cli._build_parser().parse_args(["continue", "--project", "p"])

    assert await cli._dispatch_command(args) == 0
    assert runtime.calls == [("message", "/resume")]
    assert capsys.readouterr().out.strip() == "status=RUNNING"
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

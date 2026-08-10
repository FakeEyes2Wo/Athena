"""src/main.py 入口测试：默认参数注入、子命令透传与 Agent 消息渲染。"""

import importlib.util
import json
import time
from pathlib import Path

import pytest

_MAIN = Path(__file__).resolve().parents[2] / "src" / "main.py"


def _load_main():
    spec = importlib.util.spec_from_file_location("athena_main", _MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_args_defaults_to_run_with_titanic() -> None:
    """无参数直接运行 → run + 临时 project + examples/titanic + 固定 intent。"""
    mod = _load_main()
    argv = mod._run_argv([])
    assert argv[0] == "run"
    assert "--project" in argv
    assert "examples/titanic" in argv
    assert mod._DEFAULT_TASK in argv


def test_user_provided_data_and_project_preserved() -> None:
    """用户显式给 --project/--data → 保留，不注入默认值。"""
    mod = _load_main()
    argv = mod._run_argv(["run", "--project", "p", "--data", "mydata"])
    assert "mydata" in argv
    assert "examples/titanic" not in argv
    assert "--project" in argv and "p" in argv


def test_other_subcommands_passthrough() -> None:
    """status/pause/... 原样透传，不注入 run 默认值。"""
    mod = _load_main()
    assert mod._run_argv(["status", "--project", "p"]) == ["status", "--project", "p"]
    assert mod._run_argv(
        ["reply", "--project", "p", "--request", "r", "--answer", "a"]
    ) == ["reply", "--project", "p", "--request", "r", "--answer", "a"]


def test_help_passthrough() -> None:
    """--help 原样透传，不补 run。"""
    mod = _load_main()
    assert mod._run_argv(["--help"]) == ["--help"]


def test_render_message_skips_system_prompt() -> None:
    """system-prompt 不渲染，只渲染 user/agent/tool 消息。"""
    mod = _load_main()
    msg = {
        "kind": "request",
        "parts": [{"part_kind": "system-prompt", "content": "sys"}],
    }
    assert mod._render_message(msg) == ""


def test_render_message_user_agent_and_tool() -> None:
    """user/agent 文本与 tool-call/tool-return 都渲染为可读行。"""
    mod = _load_main()
    msg = {
        "kind": "request",
        "parts": [
            {"part_kind": "user-prompt", "content": "read data"},
            {
                "part_kind": "tool-call",
                "tool_name": "write_file",
                "args": {"path": "analysis.py"},
            },
            {"part_kind": "tool-return", "content": {"path": "analysis.py"}},
        ],
    }
    rendered = mod._render_message(msg)
    assert "user> read data" in rendered
    assert "write_file" in rendered
    assert "tool" in rendered


def test_print_new_messages_replaces_text_unsupported_by_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型输出含 GBK 不支持的字符时，CLI 继续打印而不崩溃。"""
    mod = _load_main()
    sessions = tmp_path / ".athena" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "agent_1.jsonl").write_text(
        json.dumps(
            {
                "msg": [
                    {
                        "kind": "response",
                        "parts": [{"part_kind": "text", "content": "done ✅"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class GbkStream:
        encoding = "gbk"

        def __init__(self) -> None:
            self.value = ""

        def write(self, value: str) -> int:
            value.encode(self.encoding)
            self.value += value
            return len(value)

        def flush(self) -> None:
            pass

    stream = GbkStream()
    monkeypatch.setattr(mod.sys, "stdout", stream)

    mod._print_new_messages(tmp_path)

    assert "done ?" in stream.value


@pytest.mark.asyncio
async def test_run_prints_status_only_when_snapshot_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """相同 phase/status/version 连续轮询时只打印一次状态。"""
    mod = _load_main()

    class FakeRuntime:
        def __init__(self) -> None:
            self.kernel = type("FakeKernel", (), {"list_agents": lambda _self: []})()
            self.statuses = [
                {
                    "execution": {"phase": "PREPARE", "status": "RUNNING"},
                    "state_version": 3,
                },
                {
                    "execution": {"phase": "PREPARE", "status": "RUNNING"},
                    "state_version": 3,
                },
                {
                    "execution": {"phase": "COMPLETED", "status": "RUNNING"},
                    "state_version": 4,
                },
            ]

        async def dispatch(self, method: str, _params: dict) -> dict:
            if method == "TASK_CONFIGURE":
                return {"phase": "PREPARE", "interaction_mode": "interactive"}
            if method == "RUN":
                return {
                    "execution_id": "exec_test",
                    "status": "running",
                    "phase": "PREPARE",
                }
            return self.statuses.pop(0)

        async def aclose(self) -> None:
            pass

    async def no_sleep(_delay: float) -> None:
        pass

    clock_calls = 0

    def monotonic() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 0.0 if clock_calls <= 3 else 31.0

    runtime = FakeRuntime()
    monkeypatch.setattr(mod, "_runtime", lambda _project: runtime)
    monkeypatch.setattr(mod, "_print_new_messages", lambda _project: None)
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(time, "monotonic", monotonic)
    args = mod._build_parser().parse_args(
        [
            "run",
            "--project",
            str(tmp_path),
            "--data",
            "data",
            "--task",
            "task",
        ]
    )

    assert await mod._cmd_run(args) == 0
    output = capsys.readouterr().out
    assert output.count("phase=PREPARE status=RUNNING version=3") == 1
    assert output.count("phase=COMPLETED status=RUNNING version=4") == 1
    assert "still PREPARE" not in output


@pytest.mark.asyncio
async def test_run_returns_nonzero_when_execution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supervisor 进入 FAILED 终态后，CLI 立即以非零码退出。"""
    mod = _load_main()

    class FakeRuntime:
        kernel = type("FakeKernel", (), {"list_agents": lambda _self: []})()

        async def dispatch(self, method: str, _params: dict) -> dict:
            if method == "TASK_CONFIGURE":
                return {"phase": "PREPARE", "interaction_mode": "interactive"}
            if method == "RUN":
                return {
                    "execution_id": "exec_test",
                    "status": "running",
                    "phase": "PREPARE",
                }
            return {
                "execution": {"phase": "PREPARE", "status": "FAILED"},
                "state_version": 9,
            }

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(mod, "_runtime", lambda _project: FakeRuntime())
    monkeypatch.setattr(mod, "_print_new_messages", lambda _project: None)
    args = mod._build_parser().parse_args(
        ["run", "--project", str(tmp_path), "--data", "data", "--task", "task"]
    )

    assert await mod._cmd_run(args) == 1

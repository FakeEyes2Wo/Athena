"""src/main.py 入口测试：默认参数注入、子命令透传与 Agent 消息渲染。"""

import importlib.util
from pathlib import Path

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

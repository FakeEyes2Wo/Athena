# test/unit/agent/test_generic_tools.py
import asyncio
import os
from pathlib import Path
from unittest import mock

from athena.agents.tools.generic_tools import (
    _resolve_shell,
    _run_command,
    _shell_env,
    generic_tool_registry,
)
from athena.core.tool_types import ToolContext


def _ctx() -> ToolContext:
    return ToolContext("t", "id", lambda *a: asyncio.sleep(0), asyncio.Event())


def test_shell_env_preserves_windows_home(monkeypatch) -> None:
    """子进程保留 Windows home 变量，供 conda/pathlib 正确解析用户目录。"""
    values = {
        "USERPROFILE": r"C:\Users\test",
        "HOMEDRIVE": "C:",
        "HOMEPATH": r"\Users\test",
        "SYSTEMDRIVE": "C:",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    env = _shell_env()

    assert {key: env[key] for key in values} == values


def test_resolve_shell_env_override(monkeypatch) -> None:
    """ATHENA_BASH_PATH / ATHENA_PWSH_PATH 显式覆盖探测结果。"""
    monkeypatch.setenv("ATHENA_BASH_PATH", os.__file__)
    assert _resolve_shell("bash") == os.__file__


def test_resolve_shell_env_override_missing(monkeypatch) -> None:
    """配置的 shell 路径不存在 → 明确报错，不静默回退。"""
    monkeypatch.setenv("ATHENA_BASH_PATH", r"C:\no\such\bash.exe")
    try:
        _resolve_shell("bash")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "not found" in str(exc)


def test_resolve_pwsh_unix_degrades_to_none() -> None:
    """Unix 且无 pwsh → 返回 None，由 _run_command 降级为 bash。"""
    with mock.patch(
        "athena.agents.tools.generic_tools._is_windows", return_value=False
    ):
        with mock.patch("shutil.which", return_value=None):
            assert _resolve_shell("pwsh") is None


async def test_run_command_pwsh_degrades_to_bash(tmp_path: Path) -> None:
    """pwsh 不可用时自动降级为 bash 执行（跨平台可用）。"""
    (tmp_path / "_probe.py").write_text("print('ok')", encoding="utf-8")
    # 通过环境变量把 bash 指向真实 Git Bash；mock pwsh 探测失败 → 降级到 bash。
    git_bash = r"C:\Program Files\Git\usr\bin\bash.exe"
    with mock.patch.dict(os.environ, {"ATHENA_BASH_PATH": git_bash}):
        with mock.patch(
            "athena.agents.tools.generic_tools._is_windows", return_value=False
        ):
            with mock.patch("shutil.which", return_value=None):
                result = await _run_command(
                    tmp_path,
                    "pwsh",
                    ["-NoProfile", "-NonInteractive", "-Command"],
                    "python _probe.py",
                    20,
                )
                assert result["returncode"] == 0
                assert result["stdout"].strip() == "ok"


async def test_read_file_line_range(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 6)), encoding="utf-8"
    )
    tool = generic_tool_registry(tmp_path).resolve("read_file")
    result = await tool.ainvoke(_ctx(), path="a.txt", start_line=2, end_line=3)
    assert result.data["content"] == "line2\nline3"


async def test_write_file_creates_file(tmp_path: Path) -> None:
    tool = generic_tool_registry(tmp_path).resolve("write_file")
    result = await tool.ainvoke(_ctx(), path="sub/b.txt", content="hello")
    assert result.success
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "hello"


async def test_path_escape_is_rejected(tmp_path: Path) -> None:
    tool = generic_tool_registry(tmp_path).resolve("write_file")
    result = await tool.ainvoke(_ctx(), path="../evil.txt", content="x")
    assert not result.success
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_bash_runs_and_returns_output(tmp_path: Path) -> None:
    tool = generic_tool_registry(tmp_path).resolve("bash")
    result = await tool.ainvoke(_ctx(), command="echo hello-from-bash")
    assert result.success
    assert "hello-from-bash" in result.data["stdout"]


async def test_read_file_missing_raises(tmp_path: Path) -> None:
    """缺失文件 → 抛 FileNotFoundError → ToolResult(success=False)。"""
    tool = generic_tool_registry(tmp_path).resolve("read_file")
    result = await tool.ainvoke(_ctx(), path="nope.txt")
    assert not result.success
    assert "FileNotFoundError" in result.error


async def test_registry_has_four_tools(tmp_path: Path) -> None:
    reg = generic_tool_registry(tmp_path)
    assert {t.name for t in reg.specs} == {"read_file", "write_file", "bash", "pwsh"}

# test/unit/agent/test_generic_tools.py
"""generic_tool_registry：read_file/write_file 沙箱 + runtime 接线后的 shell_command。"""

import asyncio
from pathlib import Path

from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.tool_types import ToolContext


def _ctx() -> ToolContext:
    return ToolContext("t", "id", lambda *a: asyncio.sleep(0), asyncio.Event())


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


async def test_nested_demo_workspace_allows_reports_not_runtime_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".athena" / "demo" / "workspaces" / "eda"
    root.mkdir(parents=True)
    registry = generic_tool_registry(root)
    for name in ("write_file", "append_file"):
        tool = registry.resolve(name)
        result = await tool.ainvoke(_ctx(), path="EDA_TODO.md", content="report\n")
        assert result.success
        blocked = await tool.ainvoke(_ctx(), path=".athena/state.json", content="{}")
        assert not blocked.success
        escaped = await tool.ainvoke(_ctx(), path="../outside.md", content="x")
        assert not escaped.success
    assert (root / "EDA_TODO.md").read_text(encoding="utf-8") == "report\nreport\n"


async def test_write_file_rejects_framework_owned_athena(tmp_path: Path) -> None:
    """agent 的 write_file 不得写框架私有目录 .athena/**。"""
    tool = generic_tool_registry(tmp_path).resolve("write_file")
    result = await tool.ainvoke(_ctx(), path=".athena/state.json", content="{}")
    assert not result.success
    assert ".athena" in result.error
    assert not (tmp_path / ".athena" / "state.json").exists()


async def test_append_file_builds_a_long_file_in_pieces(tmp_path: Path) -> None:
    """长文件分段落盘——这是超出输出上限时唯一可行的写法。"""
    tool = generic_tool_registry(tmp_path).resolve("append_file")
    first = await tool.ainvoke(_ctx(), path="report.md", content="# Title\n")
    assert first.success
    assert first.data["total_bytes"] == len("# Title\n")

    second = await tool.ainvoke(_ctx(), path="report.md", content="## Section 1\n")
    assert second.data["appended_bytes"] == len("## Section 1\n".encode())
    assert second.data["total_bytes"] == len("# Title\n## Section 1\n")
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == (
        "# Title\n## Section 1\n"
    )


async def test_append_file_creates_missing_parents(tmp_path: Path) -> None:
    tool = generic_tool_registry(tmp_path).resolve("append_file")
    result = await tool.ainvoke(_ctx(), path="sub/dir/c.md", content="x")
    assert result.success
    assert (tmp_path / "sub" / "dir" / "c.md").read_text(encoding="utf-8") == "x"


async def test_append_file_honours_the_same_sandbox(tmp_path: Path) -> None:
    """追加工具不能成为绕开沙箱的后门。"""
    tool = generic_tool_registry(tmp_path).resolve("append_file")
    escaped = await tool.ainvoke(_ctx(), path="../evil.txt", content="x")
    assert not escaped.success
    assert not (tmp_path.parent / "evil.txt").exists()

    framework = await tool.ainvoke(_ctx(), path=".athena/state.json", content="{}")
    assert not framework.success
    assert ".athena" in framework.error


async def test_read_file_missing_raises(tmp_path: Path) -> None:
    """缺失文件 → 抛 FileNotFoundError → ToolResult(success=False)。"""
    tool = generic_tool_registry(tmp_path).resolve("read_file")
    result = await tool.ainvoke(_ctx(), path="nope.txt")
    assert not result.success
    assert "FileNotFoundError" in result.error


def test_registry_without_runtime_has_only_files(tmp_path: Path) -> None:
    """无 runtime（迁移前旧调用）只提供文件工具，不再有 bash/pwsh。"""
    reg = generic_tool_registry(tmp_path)
    assert {t.name for t in reg.specs} == {"read_file", "write_file", "append_file"}


def test_generic_registry_runtime_adds_shell_command(tmp_path: Path) -> None:
    """runtime 提供时注册 shell_command，且不再暴露 bash/pwsh（迁移步骤 5）。"""
    from athena.execution.runtime import ExecutionRuntime

    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    reg = generic_tool_registry(tmp_path, runtime=runtime)
    names = {spec.name for spec in reg.specs}
    assert names == {"read_file", "write_file", "append_file", "shell_command"}


def test_build_llm_agent_injects_runtime_summary(tmp_path: Path) -> None:
    """runtime 提供时 system prompt 注入运行时摘要，tools 含 shell_command。"""
    from athena.agents.prompt_agent import build_llm_agent
    from athena.execution.runtime import ExecutionRuntime

    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    agent = build_llm_agent(
        "data", model="test", client=None, workspace=tmp_path, runtime=runtime
    )
    assert agent.system_prompt.startswith("Runtime:")
    assert "shell_command" in {spec.name for spec in agent.tools.specs}

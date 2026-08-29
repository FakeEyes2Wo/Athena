# test/unit/agent/test_generic_tools.py
"""generic_tool_registry：read_file/write_file 沙箱 + runtime 接线后的 shell_command。"""

import asyncio
from pathlib import Path

from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
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


async def test_write_file_rejects_framework_owned_athena(tmp_path: Path) -> None:
    """agent 的 write_file 不得写框架私有目录 .athena/**。"""
    tool = generic_tool_registry(tmp_path).resolve("write_file")
    result = await tool.ainvoke(_ctx(), path=".athena/state.json", content="{}")
    assert not result.success
    assert ".athena" in result.error
    assert not (tmp_path / ".athena" / "state.json").exists()


async def test_read_file_missing_raises(tmp_path: Path) -> None:
    """缺失文件 → 抛 FileNotFoundError → ToolResult(success=False)。"""
    tool = generic_tool_registry(tmp_path).resolve("read_file")
    result = await tool.ainvoke(_ctx(), path="nope.txt")
    assert not result.success
    assert "FileNotFoundError" in result.error


def test_registry_without_runtime_has_only_files(tmp_path: Path) -> None:
    """无 runtime（迁移前旧调用）只提供文件工具，不再有 bash/pwsh。"""
    reg = generic_tool_registry(tmp_path)
    assert {t.name for t in reg.specs} == {"read_file", "write_file"}


def test_generic_registry_runtime_adds_shell_command(tmp_path: Path) -> None:
    """runtime 提供时注册 shell_command，且不再暴露 bash/pwsh（迁移步骤 5）。"""
    from athena.execution.runtime import ExecutionRuntime

    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    reg = generic_tool_registry(tmp_path, runtime=runtime)
    names = {spec.name for spec in reg.specs}
    assert names == {"read_file", "write_file", "shell_command"}


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


def test_registered_prompt_agent_injects_runtime_summary(tmp_path: Path) -> None:
    """Factory-created workers receive the same runtime block as direct agents."""
    from athena.agents.general_agent import register_general_agent
    from athena.execution.runtime import ExecutionRuntime

    registry = AgentTypeRegistry()
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    register_general_agent(
        registry,
        provider=object(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        project_root=tmp_path,
        runtime=runtime,
    )

    spec = registry.require_spec("general", agent_id="general-test")

    assert spec.runner._agent.system_prompt.startswith("Runtime:")
    assert "native PowerShell only" in spec.runner._agent.system_prompt

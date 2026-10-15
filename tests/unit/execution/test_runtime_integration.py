"""shared-execution-runtime 集成验证：wired agent 工具链端到端可用（design §Integration）。

验证：内层 LLM agent（经 ``build_llm_agent`` + ``ExecutionRuntime`` 接线）的工具集为
read_file/write_file/shell_command（无 bash/pwsh），且三者都能真实工作。
"""

import asyncio

import pytest

from athena.agents.prompt_agent import build_llm_agent
from athena.agents.task_agents import PLAN_AGENT_TYPE, register_plan_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool_types import ToolContext
from athena.execution.runtime import ExecutionRuntime

PY = "python"


def _ctx() -> ToolContext:
    return ToolContext("t", "id", lambda *a: asyncio.sleep(0), asyncio.Event())


def _wired_agent(tmp_path):
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    return build_llm_agent(
        "data", model="test", client=None, workspace=tmp_path, runtime=runtime
    )


def test_wired_agent_toolset_is_read_write_shell(tmp_path) -> None:
    """design §Integration：wired agent 收到 shell_command + 运行时上下文，无 bash/pwsh。"""
    agent = _wired_agent(tmp_path)
    assert {s.name for s in agent.tools.specs} == {
        "read_file",
        "write_file",
        "append_file",
        "shell_command",
    }
    assert agent.system_prompt.startswith("Runtime:")


@pytest.mark.asyncio
async def test_wired_agent_file_tools_work(tmp_path) -> None:
    """wired agent 的 read_file/write_file 在 workspace 沙箱内真实读写。"""
    agent = _wired_agent(tmp_path)
    wf = agent.tools.resolve("write_file")
    result = await wf.ainvoke(_ctx(), path="note.txt", content="hello wired")
    assert result.success
    rf = agent.tools.resolve("read_file")
    read = await rf.ainvoke(_ctx(), path="note.txt")
    assert "hello wired" in read.data["content"]


@pytest.mark.asyncio
async def test_wired_agent_shell_command_runs(tmp_path) -> None:
    """wired agent 的 shell_command 可真实执行命令并返回契约 JSON。"""
    agent = _wired_agent(tmp_path)
    tool = agent.tools.resolve("shell_command")
    result = await tool.ainvoke(_ctx(), command=f'{PY} -c "print(42)"')
    assert result.success
    assert result.data["ok"] is True
    assert result.data["exit_code"] == 0
    assert "42" in result.data["stdout"]


@pytest.mark.asyncio
async def test_plan_tools_share_isolated_worktree_and_reject_external_workdir(
    tmp_path,
) -> None:
    """Each Plan's read/write/shell tools use its own worktree, not task root."""
    task_root = tmp_path / "task-workspace"
    plan_roots = {
        "hyp-a": task_root / "workspaces" / "hyp-a",
        "hyp-b": task_root / "workspaces" / "hyp-b",
    }
    for plan_id, root in plan_roots.items():
        root.mkdir(parents=True)
        (root / "subdir").mkdir()
        (root / "marker.txt").write_text(plan_id, encoding="utf-8")

    runtime = ExecutionRuntime(project_root=task_root, environment_root=task_root)
    registry = AgentTypeRegistry()
    register_plan_agent(
        registry,
        provider=object(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        workspace_for=lambda agent_id: plan_roots[agent_id],
        execution=runtime,
    )

    ctx = _ctx()
    for plan_id, root in plan_roots.items():
        tools = registry.require_spec(
            PLAN_AGENT_TYPE, agent_id=plan_id
        ).runner._base_tools
        read = await tools.resolve("read_file").ainvoke(ctx, path="marker.txt")
        assert read.success and read.data["content"] == plan_id

        shell = tools.resolve("shell_command")
        command = 'python -c "import os; print(os.getcwd())"'
        default = await shell.ainvoke(ctx, command=command)
        nested = await shell.ainvoke(ctx, command=command, workdir="subdir")
        assert default.success and str(root.resolve()) in default.data["stdout"]
        assert (
            nested.success and str((root / "subdir").resolve()) in nested.data["stdout"]
        )

        escaped = await shell.ainvoke(ctx, command="pwd", workdir=str(task_root))
        assert not escaped.success
        assert "isolated workspace" in escaped.error

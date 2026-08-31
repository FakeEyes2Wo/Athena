"""shared-execution-runtime 集成验证：wired agent 工具链端到端可用（design §Integration）。

验证：内层 LLM agent（经 ``build_llm_agent`` + ``ExecutionRuntime`` 接线）的工具集为
read_file/write_file/shell_command（无 bash/pwsh），且三者都能真实工作。
"""

import asyncio

import pytest

from athena.agents.prompt_agent import build_llm_agent
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

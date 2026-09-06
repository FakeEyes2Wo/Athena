"""AgentRuntime.reap — physical garbage collection for one-shot subagents."""

import pytest

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.types import AgentCommandError, AgentStatus, ErrorCode

from ._support import make_runtime


@pytest.mark.asyncio
async def test_reap_removes_agent_and_run_history(tmp_path) -> None:
    rt: AgentRuntime = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "once"})
    await rt.wait_run(run_id, timeout=5)

    await rt.reap(agent_id)

    assert rt.has_agent(agent_id) is False
    assert rt.agent_status(agent_id) == AgentStatus.CLOSED
    assert rt.agent_snapshot(agent_id) is None
    assert rt.run_summary(run_id) is None
    assert agent_id not in {s.agent_id for s in rt.list_agents()}
    await rt.aclose()


@pytest.mark.asyncio
async def test_reap_is_idempotent(tmp_path) -> None:
    rt: AgentRuntime = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "once"})
    await rt.wait_run(run_id, timeout=5)

    await rt.reap(agent_id)
    await rt.reap(agent_id)

    assert rt.has_agent(agent_id) is False
    await rt.aclose()


@pytest.mark.asyncio
async def test_reap_unknown_agent_is_noop(tmp_path) -> None:
    rt: AgentRuntime = make_runtime(tmp_path)

    await rt.reap("does-not-exist")

    assert rt.has_agent("does-not-exist") is False
    await rt.aclose()


@pytest.mark.asyncio
async def test_reap_recursive_removes_subtree(tmp_path) -> None:
    rt: AgentRuntime = make_runtime(tmp_path)
    parent_id, run1 = await rt.create_root("stub", {"content": "parent"})
    await rt.wait_run(run1, timeout=5)
    child_id, run2 = await rt.spawn(parent_id, "stub", {"content": "child"})
    await rt.wait_run(run2, timeout=5)

    await rt.reap(parent_id, recursive=True)

    assert rt.has_agent(parent_id) is False
    assert rt.has_agent(child_id) is False
    assert all(s.agent_id not in {parent_id, child_id} for s in rt.list_agents())
    await rt.aclose()


@pytest.mark.asyncio
async def test_reap_nonrecursive_rejects_live_descendants(tmp_path) -> None:
    rt: AgentRuntime = make_runtime(tmp_path)
    parent_id, run1 = await rt.create_root("stub", {"content": "parent"})
    await rt.wait_run(run1, timeout=5)
    child_id, run2 = await rt.spawn(parent_id, "stub", {"content": "child"})
    await rt.wait_run(run2, timeout=5)

    with pytest.raises(AgentCommandError) as raised:
        await rt.reap(parent_id)

    assert raised.value.code == ErrorCode.BUSY
    assert rt.has_agent(parent_id) is True
    assert rt.has_agent(child_id) is True
    await rt.aclose()

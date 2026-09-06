import asyncio

import pytest

from ._support import BlockingAgent, StubAgent, make_runtime

from athena.agents.base_runner import BaseAgentRunner

from athena.core.agent.agent_runtime import AgentRuntime

from athena.core.agent.types import JsonCodec
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import (
    AgentCommandError,
    AgentSpec,
    AgentStatus,
    ErrorCode,
    RunStatus,
)
from athena.core.artifact_store import LocalArtifactStore


async def test_wait_for_resolves_when_target_terminal(tmp_path):
    rt = make_runtime(tmp_path)
    waiter, run_w = await rt.create_root("stub", {"content": "w"})
    target, run_t = await rt.create_root("stub", {"content": "t"})
    await rt.wait_for(waiter, [target])
    assert rt.agent_status(waiter) == AgentStatus.WAITING
    await rt.wait_run(run_t, timeout=5)  # target 完成 → 终态回调 → 解析 → 空唤醒 waiter
    for _ in range(100):
        if rt.agent_status(waiter) != AgentStatus.WAITING:
            break
        await asyncio.sleep(0.01)
    assert rt.agent_status(waiter) != AgentStatus.WAITING
    await rt.aclose()


async def test_wait_for_human_then_reply_wakes(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "q"})
    request_id = await rt.wait_for_human(agent_id, "need input")
    assert rt.agent_status(agent_id) == AgentStatus.WAITING_FOR_HUMAN
    new_run = await rt.human_reply(request_id, "answer")
    summary = await rt.wait_run(new_run, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert rt.agent_status(agent_id) != AgentStatus.WAITING_FOR_HUMAN
    await rt.aclose()


async def test_human_reply_unknown_raises(tmp_path):
    rt = make_runtime(tmp_path)
    with pytest.raises(AgentCommandError) as ei:
        await rt.human_reply("nope", "x")
    assert ei.value.code == ErrorCode.NOT_FOUND
    await rt.aclose()


async def test_wait_for_registers_when_target_starts_new_turn_after_terminal(
    tmp_path,
):
    """回归 C1: target 完成首 turn 后又启动阻塞 turn 时,_is_terminal 不得因
    _last_terminal 为终态而提前满足;wait_for 必须登记等待(waiter → WAITING)。"""
    gate = asyncio.Event()
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    registry.register(
        "block",
        lambda aid: AgentSpec(
            runner=BaseAgentRunner(BlockingAgent(gate)), codec=JsonCodec()
        ),
    )
    registry.register(
        "stub",
        lambda aid: AgentSpec(
            runner=BaseAgentRunner(StubAgent(store)), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    waiter, _ = await rt.create_root("stub", {"content": "w"})
    target, run1 = await rt.create_root("block", {"content": "t1"})
    await asyncio.wait_for(gate.wait(), timeout=2)  # 首 turn 进入运行
    await rt.interrupt(target, "first turn done")  # 首 turn 以 INTERRUPTED 终态
    summary = await rt.wait_run(run1, timeout=5)
    assert summary.status == RunStatus.INTERRUPTED
    gate.clear()
    run2 = await rt.followup(target, {"content": "t2"})  # 第二 turn 阻塞运行中
    await asyncio.wait_for(gate.wait(), timeout=2)
    assert rt._active_turn.get(target) == run2
    await rt.wait_for(waiter, [target])
    assert rt.agent_status(waiter) == AgentStatus.WAITING  # 未提前满足
    await rt.interrupt(target, "test stop")
    await rt.wait_run(run2, timeout=5)
    await rt.aclose()

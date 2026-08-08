import asyncio

import pytest

from ._support import make_runtime

# COMPAT: Task 6 迁移后改 from athena.core.agent.types import ...
from athena.core.agent_kernel.types import (
    AgentCommandError,
    AgentStatus,
    ErrorCode,
    RunStatus,
)


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

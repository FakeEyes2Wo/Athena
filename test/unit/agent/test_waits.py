"""持久化等待（WAITING / WAITING_FOR_HUMAN）在 AgentRuntime 门面上的语义（设计 §7.2/§7.3）。

Task 4 的 WaitRegistry：wait_for / wait_for_human 由门面登记（supervisor 工具经
session.kernel 注入），等待登记后当前 turn 干净结束、不占用执行槽；目标终态或
人工回复后经空唤醒恢复。
"""

import asyncio

import pytest

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.codec import JsonCodec
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, AgentStatus, RunStatus

from ._support import BlockingRunner, EchoRunner, eventually


def _runtime(tmp_path, types: dict[str, object]) -> AgentRuntime:
    registry = AgentTypeRegistry()
    for name, runner in types.items():
        registry.register(
            name,
            lambda _aid, _cfg=None, r=runner: AgentSpec(runner=r, codec=JsonCodec()),
        )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return rt


class HumanFlowRunner:
    """每轮读取 mailbox；唤醒 turn 把人工回复记入 replies。"""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def run(self, request, *, session, emit) -> dict:
        msgs = session.receive_messages()
        self.replies.extend(m.content for m in msgs)
        return {"stage": "done"}


@pytest.mark.asyncio
async def test_wait_for_human_persists_and_reply_wakes(tmp_path) -> None:
    runner = HumanFlowRunner()
    rt = _runtime(tmp_path, {"echo": runner})
    agent_id, run1 = await rt.create_root("echo", {"content": "start"})
    await rt.wait_run(run1, timeout=5)
    assert rt.agent_status(agent_id) == AgentStatus.IDLE

    request_id = await rt.wait_for_human(agent_id, "是否继续？")
    assert rt.agent_status(agent_id) == AgentStatus.WAITING_FOR_HUMAN

    run2 = await rt.human_reply(request_id, "approved")
    summary = await rt.wait_run(run2, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert runner.replies == ["approved"]  # 回复经 mailbox 到达
    assert rt.agent_status(agent_id) == AgentStatus.IDLE
    await rt.aclose()


@pytest.mark.asyncio
async def test_wait_for_wakes_when_all_children_complete(tmp_path) -> None:
    blocker1 = BlockingRunner()
    blocker2 = BlockingRunner()
    rt = _runtime(
        tmp_path, {"echo": EchoRunner(), "block1": blocker1, "block2": blocker2}
    )
    parent, _ = await rt.create_root("echo", {"content": "p"})
    c1, _ = await rt.spawn(parent, "block1", {"content": "c1"})
    c2, _ = await rt.spawn(parent, "block2", {"content": "c2"})
    await blocker1.started.wait()
    await blocker2.started.wait()
    await rt.wait_for(parent, [c1, c2])
    assert rt.agent_status(parent) == AgentStatus.WAITING  # 等待登记，不占执行槽

    blocker1.release.set()
    blocker2.release.set()
    assert await eventually(lambda: rt.agent_status(parent) != AgentStatus.WAITING)
    assert rt.agent_status(parent) == AgentStatus.IDLE  # 全部完成 → 父被空唤醒
    await rt.aclose()


@pytest.mark.asyncio
async def test_wait_for_idle_target_wakes_immediately(tmp_path) -> None:
    rt = _runtime(tmp_path, {"echo": EchoRunner()})
    waiter, run_w = await rt.create_root("echo", {"content": "w"})
    await rt.wait_run(run_w, timeout=5)
    target, run_t = await rt.create_root("echo", {"content": "t"})
    await rt.wait_run(run_t, timeout=5)  # 目标已终态
    await rt.wait_for(waiter, [target])
    assert rt.agent_status(waiter) == AgentStatus.IDLE  # 目标已终态 → 不登记等待
    await rt.aclose()

"""持久化等待（WAITING / WAITING_FOR_HUMAN）测试（设计 §7.2/§7.3）。"""

import pytest

from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.types import (
    AgentBusyError,
    AgentCommandError,
    AgentSpec,
    AgentStatus,
    ErrorCode,
    RunStatus,
)

from ._support import EchoRunner, JsonCodec, eventually


def _type(kernel, runner=None) -> str:
    """注册一个新 spec 到 kernel 的 type registry 并返回其 agent_type。"""
    spec = AgentSpec(runner=runner or EchoRunner(), codec=JsonCodec())
    t = f"t{len(kernel._type_registry.types)}"
    kernel._type_registry.register(t, lambda _aid, _cfg=None: spec)
    return t


def _kernel(**kw) -> AgentKernel:
    return AgentKernel(resources_factory=InMemoryResourcesFactory(), **kw)


class HumanFlowRunner:
    """首轮登记人工等待；唤醒后读取 mailbox 回复。"""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def run(self, request, *, session, emit) -> dict:
        self.replies.extend(str(m.content) for m in session.receive_messages())
        if not self.replies:
            await session.wait_for_human("是否继续？")
            return {"stage": "waiting"}
        return {"stage": "resumed", "replies": self.replies}


@pytest.mark.asyncio
async def test_wait_for_human_persists_and_releases_slot() -> None:
    kernel = _kernel()
    await kernel.start()
    runner = HumanFlowRunner()
    agent_id, run1 = await kernel.create_root(_type(kernel, runner), {})
    summary = await kernel.wait_run(run1, timeout=2)
    assert summary.status == RunStatus.COMPLETED  # turn 在等待边界结束
    assert kernel.agent_status(agent_id) == AgentStatus.WAITING_FOR_HUMAN
    wait = kernel._store.waits()[agent_id]
    assert wait.kind == "human"
    assert wait.request_id is not None
    assert wait.content == "是否继续？"
    assert kernel._scheduler.active_count == 0  # 等待释放执行槽
    with pytest.raises(AgentBusyError):
        await kernel.followup(agent_id, {})  # 等待中拒绝 followup
    await kernel.aclose()


@pytest.mark.asyncio
async def test_human_reply_wakes_agent_with_new_turn() -> None:
    kernel = _kernel()
    await kernel.start()
    runner = HumanFlowRunner()
    agent_id, run1 = await kernel.create_root(_type(kernel, runner), {})
    await kernel.wait_run(run1, timeout=2)
    request_id = kernel._store.waits()[agent_id].request_id
    assert request_id is not None

    run2 = await kernel.human_reply(request_id, "approved")
    assert run2 != run1
    summary = await kernel.wait_run(run2, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert runner.replies == ["approved"]  # 回复经 mailbox 到达
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    assert kernel._store.waits() == {}  # 等待已清除
    await kernel.aclose()


@pytest.mark.asyncio
async def test_human_reply_unknown_request_is_rejected() -> None:
    kernel = _kernel()
    await kernel.start()
    with pytest.raises(AgentCommandError) as raised:
        await kernel.human_reply("nonexistent", "x")
    assert raised.value.code == ErrorCode.NOT_FOUND
    await kernel.aclose()


@pytest.mark.asyncio
async def test_wait_for_human_survives_recovery() -> None:
    kernel = _kernel()
    await kernel.start()
    runner = HumanFlowRunner()
    agent_id, run1 = await kernel.create_root(_type(kernel, runner), {})
    await kernel.wait_run(run1, timeout=2)
    request_id = kernel._store.waits()[agent_id].request_id
    assert request_id is not None
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    await kernel.aclose()

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot,
        journal=journal,
        resources_factory=InMemoryResourcesFactory(),
        type_registry=kernel._type_registry,
        max_active_agents=8,
    )
    await recovered.start()
    assert recovered.agent_status(agent_id) == AgentStatus.WAITING_FOR_HUMAN
    assert recovered._store.waits()[agent_id].request_id == request_id
    # 断线/重启后仍可唤醒
    run2 = await recovered.human_reply(request_id, "approved")
    summary = await recovered.wait_run(run2, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert runner.replies == ["approved"]
    await recovered.aclose()


class ParentWaiterRunner:
    """父 runner：spawn 两个子 Agent 后 wait_for 它们；唤醒 turn 置 woke 标志。"""

    def __init__(self, kernel: AgentKernel) -> None:
        self.kernel = kernel
        self.woke = False

    async def run(self, request, *, session, emit) -> dict:
        if not request:
            self.woke = True  # 唤醒 turn（空请求触发）
            return {"stage": "resumed"}
        c1, _ = await self.kernel.spawn(
            session.agent_id, _type(self.kernel, EchoRunner()), {}, name="c1"
        )
        c2, _ = await self.kernel.spawn(
            session.agent_id, _type(self.kernel, EchoRunner()), {}, name="c2"
        )
        await session.wait_for([c1, c2])
        return {"stage": "waiting"}


@pytest.mark.asyncio
async def test_wait_for_wakes_on_all_children_complete() -> None:
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    runner = ParentWaiterRunner(kernel)
    agent_id, run1 = await kernel.create_root(_type(kernel, runner), {"start": True})
    summary = await kernel.wait_run(run1, timeout=2)
    assert summary.status == RunStatus.COMPLETED  # turn 在等待边界结束
    assert kernel.agent_status(agent_id) == AgentStatus.WAITING
    wait = kernel._store.waits()[agent_id]
    assert wait.kind == "agents"
    assert len(wait.target_ids) == 2
    # 子 Agent 全部完成 → 父被唤醒并完成唤醒 turn
    assert await eventually(lambda: kernel.agent_status(agent_id) == AgentStatus.IDLE)
    assert runner.woke
    assert kernel._store.waits() == {}
    await kernel.aclose()


class IdleTargetWaiterRunner:
    """父 runner：进程内等子完成后 wait_for 已空闲目标 → 应立即唤醒。"""

    def __init__(self, kernel: AgentKernel) -> None:
        self.kernel = kernel
        self.woke = False

    async def run(self, request, *, session, emit) -> dict:
        if not request:
            self.woke = True
            return {"stage": "resumed"}
        c, _ = await self.kernel.spawn(
            session.agent_id, _type(self.kernel, EchoRunner()), {}, name="c"
        )
        await session.wait_agents([c], timeout=2)  # 进程内等待子完成
        await session.wait_for([c])  # c 已空闲 → 立即唤醒
        return {"stage": "waiting"}


@pytest.mark.asyncio
async def test_wait_for_already_idle_target_wakes_immediately() -> None:
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    runner = IdleTargetWaiterRunner(kernel)
    agent_id, run1 = await kernel.create_root(_type(kernel, runner), {"start": True})
    await kernel.wait_run(run1, timeout=2)
    # 目标已空闲 → 不等 completion 立即唤醒
    assert await eventually(lambda: kernel.agent_status(agent_id) == AgentStatus.IDLE)
    assert runner.woke
    assert kernel._store.waits() == {}
    await kernel.aclose()

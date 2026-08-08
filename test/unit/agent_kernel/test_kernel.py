import asyncio

import pytest

from athena.core.agent_kernel.kernel import AgentKernel, AgentRegistry, AgentScheduler
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.store import (
    AgentGraphStore,
    AgentRecord,
    RunRecord,
    StoreSnapshot,
)
from athena.core.agent_kernel.types import (
    AgentBusyError,
    AgentCommandError,
    AgentSpec,
    AgentStatus,
    AgentWaitResult,
    ErrorCode,
    ReturnWhen,
    RunStatus,
)

from ._support import (
    BlockingRunner,
    EchoRunner,
    JsonCodec,
    collect_events,
)


def _type(kernel, runner=None, codec=None) -> str:
    """注册一个新 spec 到 kernel 的 type registry 并返回其 agent_type。"""
    spec = AgentSpec(runner=runner or EchoRunner(), codec=codec or JsonCodec())
    t = f"t{len(kernel._type_registry.types)}"
    kernel._type_registry.register(t, lambda _aid, _cfg=None: spec)
    return t


def _kernel(**kw) -> AgentKernel:
    return AgentKernel(resources_factory=InMemoryResourcesFactory(), **kw)


def test_scheduler_bounds_and_fifo() -> None:
    sched = AgentScheduler(max_active_agents=1)
    sched.enqueue("a")
    sched.enqueue("b")
    assert sched.try_dispatch() == "a"
    assert sched.try_dispatch() is None  # 容量满
    sched.release_active("a")
    assert sched.try_dispatch() == "b"


def test_scheduler_park_releases_lease() -> None:
    sched = AgentScheduler(max_active_agents=1)
    sched.enqueue("parent")
    assert sched.try_dispatch() == "parent"
    sched.park("parent")
    sched.enqueue("child")
    assert sched.try_dispatch() == "child"


@pytest.mark.asyncio
async def test_acquire_lease_wakes_when_capacity_frees() -> None:
    sched = AgentScheduler(max_active_agents=1)
    sched.enqueue("busy")
    sched.try_dispatch()
    waiter = asyncio.create_task(sched.acquire_lease("waiting"))
    await asyncio.sleep(0)
    assert not waiter.done()  # 容量满 → 等待
    sched.release_active("busy")
    await asyncio.wait_for(waiter, timeout=1)
    assert sched.active_count == 1


def test_registry_path_and_subtree() -> None:
    store = AgentGraphStore()
    registry = AgentRegistry(store)
    assert registry.child_path(None, "root") == ("root",)
    store.commit(
        command_id="seed",
        kind="spawn",
        payload={
            "agent": AgentRecord(
                agent_id="p1",
                path=("root",),
                name="root",
                agent_type="agent",
                parent_id=None,
                status=AgentStatus.IDLE,
                created_sequence=1,
            ),
            "run": RunRecord(
                run_id="p1:r:1",
                agent_id="p1",
                parent_run_id=None,
                status=RunStatus.QUEUED,
                generation=0,
                request_ref="{}",
            ),
        },
    )
    # 子路径由 parent 记录的展示路径派生，不拼接不透明 id
    assert registry.child_path("p1", "c") == ("root", "c")


def test_kernel_defaults_match_spec_config() -> None:
    kernel = _kernel()
    assert kernel._scheduler.max_active_agents == 8
    assert kernel._max_spawn_depth == 4


@pytest.mark.asyncio
async def test_serializer_executes_spawn_and_idempotency() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel), {"q": 1})
    assert agent_id.startswith("agent_")  # 不透明唯一 ID
    assert kernel._store.run(run_id) is not None
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    await kernel.aclose()


@pytest.mark.asyncio
async def test_command_id_is_idempotent() -> None:
    kernel = _kernel()
    await kernel.start()
    first = await kernel._enqueue(
        "spawn",
        {"parent_id": None, "agent_type": _type(kernel), "task": {}, "name": "root"},
        command_id="cid-1",
    )
    second = await kernel._enqueue(
        "spawn",
        {"parent_id": None, "agent_type": _type(kernel), "task": {}, "name": "other"},
        command_id="cid-1",
    )
    assert first == second
    root_id, _ = first
    assert kernel.agent_status(root_id) is not None
    assert len(kernel._store.agents()) == 1  # 幂等重放不创建新实例
    await kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_allows_same_name_siblings() -> None:
    """设计 §5.1：name 与派生路径允许重复，身份唯一由不透明 agent_id 保证。"""
    kernel = _kernel()
    await kernel.start()
    root_id, _ = await kernel.create_root(_type(kernel), {})
    first_id, _ = await kernel.spawn(root_id, _type(kernel), {}, name="c")
    second_id, _ = await kernel.spawn(root_id, _type(kernel), {}, name="c")
    assert first_id != second_id  # 同名兄弟独立实例
    assert kernel.agent_status(first_id) is not None
    assert kernel.agent_status(second_id) is not None
    await kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_is_not_limited_by_agent_count() -> None:
    """设计 §6.2：Scheduler 只限制同时执行，不限制逻辑 Agent 创建数量。"""
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    root_id, _ = await kernel.create_root(_type(kernel), {})
    for i in range(5):
        await kernel.spawn(root_id, _type(kernel), {}, name=f"c{i}")
    assert len(kernel._store.agents()) == 6  # root + 5 个子 Agent
    await kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_rejects_beyond_max_depth() -> None:
    kernel = _kernel(max_spawn_depth=2)
    await kernel.start()
    root_id, _ = await kernel.create_root(_type(kernel), {})
    c1_id, _ = await kernel.spawn(root_id, _type(kernel), {}, name="c1")
    c2_id, _ = await kernel.spawn(c1_id, _type(kernel), {}, name="c2")
    with pytest.raises(AgentCommandError) as raised:
        await kernel.spawn(c2_id, _type(kernel), {}, name="c3")
    assert raised.value.code == ErrorCode.LIMIT_REACHED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_run_completes_and_resolves_wait() -> None:
    runner = EchoRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, runner), {"q": "hi"})
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert summary.response_ref == '{"echo": {"q": "hi"}}'
    assert runner.calls == [{"q": "hi"}]
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    events = await collect_events(kernel.run_events(run_id, after_sequence=0), 2)
    assert [e.kind for e in events] == ["agent/step", "run_completed"]
    await kernel.aclose()


@pytest.mark.asyncio
async def test_runner_failure_marks_run_failed_and_agent_idle() -> None:
    class FailingRunner:
        async def run(self, request, *, session, emit) -> dict:
            raise ValueError("bad request")

    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, FailingRunner()), {})
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.FAILED
    assert summary.error == "ValueError"  # 错误脱敏：仅暴露类型，不泄露异常文本
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    await kernel.aclose()


@pytest.mark.asyncio
async def test_followup_after_completion_creates_new_run() -> None:
    runner = EchoRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run1 = await kernel.create_root(_type(kernel, runner), {"q": 1})
    await kernel.wait_run(run1, timeout=2)
    run2 = await kernel.followup(agent_id, {"q": 2})
    assert run2 != run1
    summary = await kernel.wait_run(run2, timeout=2)
    assert summary.response_ref == '{"echo": {"q": 2}}'
    await kernel.aclose()


@pytest.mark.asyncio
async def test_session_events_span_runs() -> None:
    runner = EchoRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run1 = await kernel.create_root(_type(kernel, runner), {"q": 1})
    await kernel.wait_run(run1, timeout=2)
    run2 = await kernel.followup(agent_id, {"q": 2})
    await kernel.wait_run(run2, timeout=2)
    events = await collect_events(kernel.session_events(agent_id, after_sequence=0), 4)
    assert len(events) == 4  # 两轮各：agent/step + run_completed
    assert events[0].run_id == run1
    assert events[2].run_id == run2
    await kernel.aclose()


class InboxRunner:
    """记录每轮 runner 看到的 mailbox 未提交窗口。"""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    async def run(self, request, *, session, emit) -> dict:
        msgs = [str(m.content) for m in session.receive_messages()]
        self.seen.append(msgs)
        return {"seen": msgs}


@pytest.mark.asyncio
async def test_send_message_then_followup_delivers_message() -> None:
    runner = InboxRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_type(kernel, runner), {})
    await kernel.send_message(agent_id, "hello")
    run_id = await kernel.followup(agent_id, {"task": 1})
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    # create_root 的首 Run 先读到空 mailbox；followup 的 Run 读到已投递消息（§3.3）
    assert runner.seen == [[], ["hello"]]
    await kernel.aclose()


@pytest.mark.asyncio
async def test_busy_followup_raises_and_changes_nothing() -> None:
    runner = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_type(kernel, runner), {})
    await runner.started.wait()
    runs_before = {rid: r.status for rid, r in kernel._store.runs().items()}
    mailbox_before = list(kernel._store.mailbox(agent_id))
    with pytest.raises(AgentBusyError):
        await kernel.followup(agent_id, {"task": 2})
    assert {rid: r.status for rid, r in kernel._store.runs().items()} == runs_before
    assert kernel._store.mailbox(agent_id) == mailbox_before
    runner.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_child_completion_delivers_once_to_parent_mailbox() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    child_id, child_run = await kernel.spawn(
        parent_id, _type(kernel, EchoRunner()), {}, name="c"
    )
    await kernel.wait_run(child_run, timeout=2)
    completions = [m for m in kernel._store.mailbox(parent_id) if m.is_control]
    assert len(completions) == 1
    assert completions[0].content == f"child completed: {child_run}"
    assert completions[0].context_refs == [
        kernel._store.run_summary(child_run).response_ref
    ]
    await kernel.aclose()


@pytest.mark.asyncio
async def test_wait_agent_all_completed_and_timeout() -> None:
    kernel = _kernel(max_active_agents=4)
    await kernel.start()
    parent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    children = [
        await kernel.spawn(parent_id, _type(kernel, EchoRunner()), {}, name=f"c{i}")
        for i in range(3)
    ]
    result = await kernel.wait_agent(
        [cid for cid, _ in children], return_when=ReturnWhen.ALL_COMPLETED, timeout=3
    )
    assert result.timed_out is False
    assert set(result.completed) == {cid for cid, _ in children}

    blocker = BlockingRunner()
    agent_id, _ = await kernel.create_root(
        _type(kernel, blocker), {"block": 1}, name="blocker"
    )
    await blocker.started.wait()
    timeout_result = await kernel.wait_agent([agent_id], timeout=0.05)
    assert timeout_result.timed_out is True
    assert agent_id not in timeout_result.completed
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_wait_agent_first_completed_success_is_not_timeout() -> None:
    kernel = _kernel(max_active_agents=4)
    await kernel.start()
    parent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    fast_id, _ = await kernel.spawn(
        parent_id, _type(kernel, EchoRunner()), {}, name="fast"
    )
    blocker = BlockingRunner()
    slow_id, _ = await kernel.spawn(parent_id, _type(kernel, blocker), {}, name="slow")
    await blocker.started.wait()
    result = await kernel.wait_agent(
        [fast_id, slow_id], return_when=ReturnWhen.FIRST_COMPLETED, timeout=3
    )
    assert result.timed_out is False  # fast 已满足 FIRST_COMPLETED，不算超时
    assert fast_id in result.completed
    assert slow_id not in result.completed
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_interrupt_running_run_stops_runner() -> None:
    runner = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, runner), {})
    await runner.started.wait()
    await kernel.interrupt(agent_id, "stop it")
    assert runner.cancelled
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    assert kernel._store.run(run_id).status == RunStatus.INTERRUPTED
    assert kernel._store.run(run_id).reason == "stop it"
    await kernel.aclose()


@pytest.mark.asyncio
async def test_interrupt_queued_run_never_starts_runner() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    other_id, other_run = await kernel.create_root(
        _type(kernel, EchoRunner()), {}, name="other"
    )
    await kernel.wait_run(other_run, timeout=2)  # other 先回 IDLE
    await kernel.create_root(_type(kernel, blocker), {})  # blocker 占用唯一容量
    await blocker.started.wait()
    # 全局容量满 → other（空闲）的 followup Run 保持 QUEUED（§3.2）
    run2 = await kernel.followup(other_id, {})
    assert kernel._store.run(run2).status == RunStatus.QUEUED
    await kernel.interrupt(other_id, "stop queued")
    assert kernel._store.run(run2).status == RunStatus.INTERRUPTED
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_idle_and_duplicate_interrupt_are_noops() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    await kernel.interrupt(agent_id, "idle")
    await kernel.interrupt(agent_id, "again")
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    await kernel.aclose()


@pytest.mark.asyncio
async def test_cancel_run_binds_exact_run_id() -> None:
    runner = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, runner), {})
    await runner.started.wait()
    await kernel.cancel_run(run_id, reason="caller_cancelled")
    assert kernel._store.run(run_id).status == RunStatus.INTERRUPTED
    assert kernel._store.run(run_id).reason == "caller_cancelled"
    runner.release.set()
    await kernel.aclose()


class SlowUnwindRunner:
    """取消后先跨一个 await 再置 cancelled：暴露 interrupt 未等待旧 runner 的缺陷。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def run(self, request: object, *, session, emit) -> dict:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)  # 让 task.cancel() 与 task 真正结束分离
            self.cancelled = True
            raise
        return {"done": True}


class ParkedWaiterRunner:
    """启动后经 session.wait_agents 等待目标终结（PARKED 场景）。"""

    def __init__(self, target_id) -> None:
        self.started = asyncio.Event()
        self.target_id = target_id

    async def run(self, request: object, *, session, emit) -> dict:
        self.started.set()
        await session.wait_agents([self.target_id], timeout=None)
        return {"waited": True}


@pytest.mark.asyncio
async def test_interrupt_returns_only_after_runner_stopped() -> None:
    runner = SlowUnwindRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, runner), {})
    await runner.started.wait()
    await kernel.interrupt(agent_id, "stop it")
    assert runner.cancelled  # 无修复时 interrupt 提前返回 → 此处为 False
    assert kernel._store.run(run_id).status == RunStatus.INTERRUPTED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_interrupt_parked_run_releases_lease() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_agents=2)
    await kernel.start()
    target_id, _ = await kernel.create_root(_type(kernel, blocker), {})
    await blocker.started.wait()
    waiter_runner = ParkedWaiterRunner(target_id)
    waiter_id, waiter_run = await kernel.create_root(
        _type(kernel, waiter_runner), {}, name="waiter"
    )
    await waiter_runner.started.wait()
    await kernel.interrupt(waiter_id, "stop parked")
    assert kernel._store.run(waiter_run).status == RunStatus.INTERRUPTED
    assert waiter_run not in kernel._scheduler._active  # lease 已释放
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_close_nonrecursive_fails_with_live_descendants() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    child_id, _ = await kernel.spawn(
        parent_id, _type(kernel, EchoRunner()), {}, name="c"
    )
    with pytest.raises(AgentCommandError) as raised:
        await kernel.close(parent_id)
    assert raised.value.code == ErrorCode.INVALID_REQUEST
    assert kernel.agent_status(parent_id) == AgentStatus.IDLE
    assert kernel.agent_status(child_id) == AgentStatus.IDLE
    await kernel.aclose()


@pytest.mark.asyncio
async def test_close_recursive_closes_postorder_and_rejects_new_commands() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    child_id, _ = await kernel.spawn(
        parent_id, _type(kernel, EchoRunner()), {}, name="c"
    )
    await kernel.close(parent_id, recursive=True)
    assert kernel.agent_status(child_id) == AgentStatus.CLOSED
    assert kernel.agent_status(parent_id) == AgentStatus.CLOSED
    assert kernel._scheduler.active_count == 0
    with pytest.raises(AgentCommandError) as raised:
        await kernel.followup(child_id, {})
    assert raised.value.code == ErrorCode.CLOSED
    with pytest.raises(AgentCommandError) as raised:
        await kernel.send_message(parent_id, "late")
    assert raised.value.code == ErrorCode.CLOSED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_duplicate_close_is_idempotent() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    await kernel.close(agent_id)
    await kernel.close(agent_id)
    assert kernel.agent_status(agent_id) == AgentStatus.CLOSED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_all_agents() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    await kernel.spawn(parent_id, _type(kernel, EchoRunner()), {}, name="c")
    await kernel.aclose()
    assert kernel._scheduler.active_count == 0
    assert all(a.status == AgentStatus.CLOSED for a in kernel._store.agents().values())


@pytest.mark.asyncio
async def test_close_queued_agent_never_dispatches() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    await kernel.create_root(_type(kernel, blocker), {})
    await blocker.started.wait()
    # 容量满 → 第二棵 root 的初始 Run 保持 QUEUED（§3.2 pending_run_id 已记录）
    other_id, other_run = await kernel.create_root(
        _type(kernel, EchoRunner()), {}, name="other"
    )
    assert kernel._store.run(other_run).status == RunStatus.QUEUED
    await kernel.close(other_id, recursive=True)
    assert kernel.agent_status(other_id) == AgentStatus.CLOSED
    assert kernel._store.run(other_run).status == RunStatus.INTERRUPTED
    blocker.release.set()
    await asyncio.sleep(0)  # 释放容量后该 Run 不得被派发
    assert kernel._store.run(other_run).status == RunStatus.INTERRUPTED
    assert kernel.agent_status(other_id) == AgentStatus.CLOSED
    await kernel.aclose()


class ParentWaiter:
    """父 runner：spawn 子 Agent 后经 session.wait_agents 等它完成。"""

    def __init__(self, kernel: AgentKernel) -> None:
        self.kernel = kernel
        self.result: AgentWaitResult | None = None

    async def run(self, request, *, session, emit) -> dict:
        child_id, _ = await self.kernel.spawn(
            session.agent_id, _type(self.kernel, EchoRunner()), {}, name="kid"
        )
        self.result = await session.wait_agents([child_id], timeout=5)
        return {"child": self.result.completed[child_id].status.value}


@pytest.mark.asyncio
async def test_parent_waits_child_without_deadlock_at_one_slot() -> None:
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    runner = ParentWaiter(kernel)
    parent_id, parent_run = await kernel.create_root(_type(kernel, runner), {})
    summary = await kernel.wait_run(parent_run, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert summary.response_ref == '{"child": "completed"}'
    assert runner.result is not None
    await kernel.aclose()


@pytest.mark.asyncio
async def test_recover_replays_journal_for_completed_run() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, run1 = await kernel.create_root(_type(kernel, EchoRunner()), {"q": 1})
    await kernel.wait_run(run1, timeout=2)
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
    assert recovered.agent_status(agent_id) == AgentStatus.IDLE
    assert recovered.run_summary(run1).status == RunStatus.COMPLETED
    await recovered.aclose()


@pytest.mark.asyncio
async def test_recover_marks_crashed_running_run_as_failed() -> None:
    blocker = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, blocker), {})
    await blocker.started.wait()
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot,
        journal=journal,
        resources_factory=InMemoryResourcesFactory(),
        type_registry=kernel._type_registry,
        max_active_agents=8,
    )
    await recovered.start()
    summary = recovered.run_summary(run_id)
    assert summary is not None
    assert summary.status == RunStatus.FAILED
    assert summary.error == "kernel_restarted"
    assert recovered.agent_status(agent_id) == AgentStatus.IDLE

    blocker.release.set()
    await kernel.aclose()
    await recovered.aclose()


@pytest.mark.asyncio
async def test_recover_resumes_queued_run() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    other_id, other_run = await kernel.create_root(
        _type(kernel, EchoRunner()), {}, name="other"
    )
    await kernel.wait_run(other_run, timeout=2)  # other 先回 IDLE
    await kernel.create_root(_type(kernel, blocker), {})  # blocker 占用唯一容量
    await blocker.started.wait()
    # 全局容量满 → other（空闲）的 followup Run 保持 QUEUED（§3.2）
    queued = await kernel.followup(other_id, {})
    assert kernel._store.run(queued).status == RunStatus.QUEUED
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    blocker.release.set()
    await kernel.aclose()

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot,
        journal=journal,
        resources_factory=InMemoryResourcesFactory(),
        type_registry=kernel._type_registry,
        max_active_agents=1,
    )
    await recovered.start()
    summary = await recovered.wait_run(queued, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    await recovered.aclose()


@pytest.mark.asyncio
async def test_recover_rebuilds_all_agent_sessions() -> None:
    """设计 §6.2：恢复不因逻辑 Agent 数量受限，全部非 CLOSED Agent 重建会话。"""
    kernel = _kernel()
    await kernel.start()
    parent_id, parent_run = await kernel.create_root(_type(kernel, EchoRunner()), {})
    child_id, child_run = await kernel.spawn(
        parent_id, _type(kernel, EchoRunner()), {}, name="c"
    )
    await kernel.wait_run(parent_run, timeout=2)
    await kernel.wait_run(child_run, timeout=2)
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    await kernel.aclose()

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot,
        journal=journal,
        resources_factory=InMemoryResourcesFactory(),
        type_registry=kernel._type_registry,
        max_active_agents=1,
    )
    await recovered.start()
    assert set(recovered._sessions) == {parent_id, child_id}
    assert recovered.agent_status(parent_id) == AgentStatus.IDLE
    assert recovered.agent_status(child_id) == AgentStatus.IDLE
    await recovered.aclose()


@pytest.mark.asyncio
async def test_aclose_waits_for_runner_quiescence() -> None:
    runner = SlowUnwindRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_type(kernel, runner), {})
    await runner.started.wait()
    await kernel.aclose()
    assert runner.cancelled  # aclose 返回前 runner 已静止（B5）


@pytest.mark.asyncio
async def test_wait_all_completed_partial_timeout_is_timed_out() -> None:
    kernel = _kernel(max_active_agents=4)
    await kernel.start()
    fast_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    blocker = BlockingRunner()
    slow_id, _ = await kernel.create_root(_type(kernel, blocker), {}, name="slow")
    await blocker.started.wait()
    result = await kernel.wait_agent(
        [fast_id, slow_id], return_when=ReturnWhen.ALL_COMPLETED, timeout=0.1
    )
    assert result.timed_out is True  # 部分完成仍算超时（B9）
    assert fast_id in result.completed
    assert slow_id not in result.completed
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_park_lock_cleaned_after_wait() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_agents=2)
    await kernel.start()
    target_id, _ = await kernel.create_root(_type(kernel, blocker), {})
    await blocker.started.wait()
    waiter = ParkedWaiterRunner(target_id)
    waiter_id, waiter_run = await kernel.create_root(
        _type(kernel, waiter), {}, name="waiter"
    )
    await waiter.started.wait()
    for _ in range(50):  # 等 waiter park 进入锁
        if waiter_run in kernel._park_locks:
            break
        await asyncio.sleep(0.01)
    assert waiter_run in kernel._park_locks
    blocker.release.set()
    await kernel.wait_run(waiter_run, timeout=2)
    assert waiter_run not in kernel._park_locks  # 已清理（B9）
    await kernel.aclose()


@pytest.mark.asyncio
async def test_codec_error_is_sanitized() -> None:
    class LeakyCodec(JsonCodec):
        def encode_request(self, value: object) -> str:
            if isinstance(value, dict) and value.get("_leak"):
                raise RuntimeError("secret-credential-value")
            return super().encode_request(value)

    kernel = _kernel()
    await kernel.start()
    spec = _type(kernel, EchoRunner(), codec=LeakyCodec())
    agent_id, first_run = await kernel.create_root(spec, {})
    await kernel.wait_run(first_run, timeout=2)  # 先回 IDLE，避免 BUSY
    with pytest.raises(AgentCommandError) as raised:
        await kernel.followup(agent_id, {"_leak": True})
    assert raised.value.code == ErrorCode.CODEC_ERROR
    assert str(raised.value) == "RuntimeError"  # 只暴露类型，防凭据泄露（B11）
    await kernel.aclose()


@pytest.mark.asyncio
async def test_recover_skips_queued_run_without_session() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    other_id, other_run = await kernel.create_root(
        _type(kernel, EchoRunner()), {}, name="other"
    )
    await kernel.wait_run(other_run, timeout=2)  # other 先回 IDLE
    root_id, root_run = await kernel.create_root(
        _type(kernel, blocker), {}
    )  # root 占容量
    await blocker.started.wait()
    queued = await kernel.followup(other_id, {})  # other（空闲）的 followup → QUEUED
    assert kernel._store.run(queued).status == RunStatus.QUEUED
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    blocker.release.set()
    await kernel.aclose()

    # root 资源重建失败 → root 进入 ERROR 无 Session；other 的 QUEUED Run 正常恢复
    class FailingFactory:
        def create(self, agent_id, spec, *, context_ref=None):
            if agent_id == root_id:
                raise RuntimeError("cannot rebuild")
            return InMemoryResourcesFactory().create(
                agent_id, spec, context_ref=context_ref
            )

        async def aclose(self):
            pass

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot,
        journal=journal,
        resources_factory=FailingFactory(),
        type_registry=kernel._type_registry,
        max_active_agents=8,
    )
    await recovered.start()  # 不得 KeyError
    assert recovered.agent_status(root_id) == AgentStatus.ERROR
    summary = await recovered.wait_run(queued, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    await recovered.aclose()


@pytest.mark.asyncio
async def test_recover_terminates_orphaned_queued_run() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_agents=1)
    await kernel.start()
    root_id, root_run = await kernel.create_root(
        _type(kernel, blocker), {}
    )  # root 占容量
    await blocker.started.wait()
    other_id, other_run = await kernel.create_root(
        _type(kernel, EchoRunner()), {}, name="other"
    )
    # 容量满 → other 的初始 Run 保持 QUEUED
    assert kernel._store.run(other_run).status == RunStatus.QUEUED
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    blocker.release.set()
    await kernel.aclose()

    # other 资源重建失败 → other 无 Session；其孤儿 QUEUED Run 被原子终结为 FAILED（B6）
    class FailingFactory:
        def create(self, agent_id, spec, *, context_ref=None):
            if agent_id == other_id:
                raise RuntimeError("cannot rebuild")
            return InMemoryResourcesFactory().create(
                agent_id, spec, context_ref=context_ref
            )

        async def aclose(self):
            pass

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot,
        journal=journal,
        resources_factory=FailingFactory(),
        type_registry=kernel._type_registry,
        max_active_agents=8,
    )
    await recovered.start()  # 不得 KeyError
    assert recovered.agent_status(other_id) == AgentStatus.ERROR
    summary = recovered.run_summary(other_run)
    assert summary is not None
    assert summary.status == RunStatus.FAILED
    await recovered.aclose()  # 不得 KeyError


@pytest.mark.asyncio
async def test_park_lock_timeout_returns_timed_out() -> None:
    blocker = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, blocker), {})
    await blocker.started.wait()
    kernel._park_locks[run_id] = asyncio.Lock()
    lock = kernel._park_locks[run_id]
    await lock.acquire()
    try:
        result = await kernel.wait_agent_parked(
            [agent_id],
            parking_run_id=run_id,
            parking_generation=1,  # 有效 Run → 走到锁等待
            timeout=0.05,
        )
    finally:
        lock.release()
        kernel._park_locks.pop(run_id, None)
    assert result.timed_out is True  # 锁等待超时 → timed_out，不抛 TimeoutError（B8）
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_send_message_builds_authoritative_envelope() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_type(kernel, EchoRunner()), {})
    await kernel.send_message(agent_id, "hello", context_refs=["ref://1"])
    messages = kernel._store.mailbox(agent_id)
    assert messages[0].source == "user"  # Kernel 生成权威 source
    assert messages[0].content == "hello"  # 消息原文保留
    assert messages[0].context_refs == ["ref://1"]  # 引用原样保留（B9）
    await kernel.aclose()


@pytest.mark.asyncio
async def test_codec_error_has_no_cause() -> None:
    class LeakyCodec(JsonCodec):
        def encode_request(self, value: object) -> str:
            raise RuntimeError("secret-credential-value")

    kernel = _kernel()
    await kernel.start()
    spec = _type(kernel, EchoRunner(), codec=LeakyCodec())
    with pytest.raises(AgentCommandError) as raised:
        await kernel.create_root(spec, {})
    assert raised.value.__cause__ is None  # 断链，secret 不可经 __cause__ 读取（B10）
    assert raised.value.__context__ is None  # __context__ 亦不泄密（R6）
    await kernel.aclose()


@pytest.mark.asyncio
async def test_events_survive_recovery() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel, EchoRunner()), {})
    await kernel.wait_run(run_id, timeout=2)
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
    # live journal 已从 Store 回填：agent/step + run_completed 两条事件（R3）
    events = await collect_events(recovered.session_events(agent_id), 2)
    kinds = [e.kind for e in events]
    assert "agent/step" in kinds
    assert "run_completed" in kinds
    await recovered.aclose()


@pytest.mark.asyncio
async def test_context_ref_written_back_on_terminal() -> None:
    """设计 §4.3：私有记忆 context_ref 在安全边界写回 AgentRecord。"""
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_type(kernel), {"q": 1})
    await kernel.wait_run(run_id, timeout=2)
    agent = kernel._store.require_agent(agent_id)
    assert agent.context_ref == f"context://{agent_id}"  # 终态后写回
    await kernel.aclose()

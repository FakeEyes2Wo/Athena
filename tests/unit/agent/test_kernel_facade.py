"""AgentKernel 公共门面行为在 AgentRuntime 上的迁移测试。

旧 ``test_kernel.py`` 中依赖 GraphStore/Scheduler/park/recovery 内部实现的
用例不迁移（Codex 风格轻持久化无图存储）；create_root/followup/interrupt/
status/events 等门面断言改写走 AgentRuntime 构造（``make_runtime`` 模式）。
"""

import asyncio

import pytest

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import (
    AgentBusyError,
    AgentCommandError,
    AgentSpec,
    AgentStatus,
    ErrorCode,
    ReturnWhen,
    RunStatus,
)

from ._support import (
    BlockingRunner,
    EchoRunner,
    JsonCodec,
    collect_events,
    collect_until_terminal,
)


def _runtime(tmp_path, runners: dict[str, object]) -> AgentRuntime:
    """按名称注册 runner 替身并构造门面。"""
    registry = AgentTypeRegistry()
    for name, runner in runners.items():
        registry.register(
            name,
            lambda _aid, r=runner: AgentSpec(runner=r, codec=JsonCodec()),
        )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return rt


class InboxRunner:
    """记录每轮 runner 看到的 mailbox 未提交窗口。"""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    async def run(self, request, *, session, emit) -> dict:
        msgs = [str(m.content) for m in session.receive_messages()]
        self.seen.append(msgs)
        return {"seen": msgs}


class SlowUnwindRunner:
    """取消后先跨一个 await 再置 cancelled：暴露 interrupt 未等待旧 runner 的缺陷。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, request, *, session, emit) -> dict:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)  # 让 task.cancel() 与 task 真正结束分离
            self.cancelled = True
            raise
        return {"done": True}


@pytest.mark.asyncio
async def test_run_completes_and_resolves_wait(tmp_path) -> None:
    runner = EchoRunner()
    rt = _runtime(tmp_path, {"echo": runner})
    agent_id, run_id = await rt.create_root("echo", {"q": "hi"})
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert summary.response_ref == '{"echo": {"q": "hi"}}'
    assert runner.calls == [{"q": "hi"}]
    assert rt.agent_status(agent_id) == AgentStatus.IDLE
    events = await collect_until_terminal(rt.run_events(run_id, after_sequence=0))
    assert events[-1].kind == "turn_completed"
    assert any(e.kind == "agent/step" for e in events)
    await rt.aclose()


@pytest.mark.asyncio
async def test_runner_failure_marks_run_failed_and_agent_error(tmp_path) -> None:
    class FailingRunner:
        async def run(self, request, *, session, emit) -> dict:
            raise ValueError("bad request")

    rt = _runtime(tmp_path, {"failing": FailingRunner()})
    agent_id, run_id = await rt.create_root("failing", {})
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.FAILED
    assert "ValueError" in summary.error  # 错误脱敏：仅暴露类型，不泄露异常文本
    assert rt.agent_status(agent_id) == AgentStatus.ERROR
    await rt.aclose()


@pytest.mark.asyncio
async def test_followup_after_completion_creates_new_run(tmp_path) -> None:
    runner = EchoRunner()
    rt = _runtime(tmp_path, {"echo": runner})
    agent_id, run1 = await rt.create_root("echo", {"q": 1})
    await rt.wait_run(run1, timeout=5)
    run2 = await rt.followup(agent_id, {"q": 2})
    assert run2 != run1
    summary = await rt.wait_run(run2, timeout=5)
    assert summary.response_ref == '{"echo": {"q": 2}}'
    await rt.aclose()


@pytest.mark.asyncio
async def test_session_events_span_runs(tmp_path) -> None:
    runner = EchoRunner()
    rt = _runtime(tmp_path, {"echo": runner})
    agent_id, run1 = await rt.create_root("echo", {"q": 1})
    await rt.wait_run(run1, timeout=5)
    run2 = await rt.followup(agent_id, {"q": 2})
    await rt.wait_run(run2, timeout=5)
    events = await collect_events(rt.session_events(agent_id, after_sequence=0), 10)
    assert events[0].run_id == run1
    kinds_by_run: dict[str, list[str]] = {}
    for e in events:
        kinds_by_run.setdefault(e.run_id, []).append(e.kind)
    assert "turn_completed" in kinds_by_run[run1]
    assert "turn_completed" in kinds_by_run[run2]
    await rt.aclose()


@pytest.mark.asyncio
async def test_send_message_then_followup_delivers_message(tmp_path) -> None:
    runner = InboxRunner()
    rt = _runtime(tmp_path, {"inbox": runner})
    agent_id, run0 = await rt.create_root("inbox", {})
    await rt.wait_run(run0, timeout=5)
    await rt.send_message(agent_id, "hello")
    run_id = await rt.followup(agent_id, {"task": 1})
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    # create_root 的首 Run 先读到空 mailbox；followup 的 Run 读到已投递消息（§3.3）
    assert runner.seen == [[], ["hello"]]
    await rt.aclose()


@pytest.mark.asyncio
async def test_busy_followup_raises_and_changes_nothing(tmp_path) -> None:
    runner = BlockingRunner()
    rt = _runtime(tmp_path, {"block": runner})
    agent_id, _ = await rt.create_root("block", {})
    await runner.started.wait()
    with pytest.raises(AgentBusyError):
        await rt.followup(agent_id, {"task": 2})
    assert rt.agent_status(agent_id) == AgentStatus.RUNNING  # 状态未被破坏
    runner.release.set()
    await rt.aclose()


@pytest.mark.asyncio
async def test_spawn_allows_same_name_siblings(tmp_path) -> None:
    """设计 §5.1：name 与派生路径允许重复，身份唯一由不透明 agent_id 保证。"""
    rt = _runtime(tmp_path, {"echo": EchoRunner()})
    root_id, _ = await rt.create_root("echo", {})
    first_id, _ = await rt.spawn(root_id, "echo", {}, name="c")
    second_id, _ = await rt.spawn(root_id, "echo", {}, name="c")
    assert first_id != second_id  # 同名兄弟独立实例
    assert rt.agent_status(first_id) is not None
    assert rt.agent_status(second_id) is not None
    await rt.aclose()


@pytest.mark.asyncio
async def test_wait_agent_all_completed_and_timeout(tmp_path) -> None:
    blocker = BlockingRunner()
    rt = _runtime(tmp_path, {"echo": EchoRunner(), "block": blocker})
    parent_id, _ = await rt.create_root("echo", {})
    children = [await rt.spawn(parent_id, "echo", {}, name=f"c{i}") for i in range(3)]
    result = await rt.wait_agent(
        [cid for cid, _ in children], return_when=ReturnWhen.ALL_COMPLETED, timeout=5
    )
    assert result.timed_out is False
    assert set(result.completed) == {cid for cid, _ in children}

    agent_id, _ = await rt.create_root("block", {"block": 1}, name="blocker")
    await blocker.started.wait()
    timeout_result = await rt.wait_agent([agent_id], timeout=0.05)
    assert timeout_result.timed_out is True
    assert agent_id not in timeout_result.completed
    blocker.release.set()
    await rt.aclose()


@pytest.mark.asyncio
async def test_wait_agent_first_completed_success_is_not_timeout(tmp_path) -> None:
    blocker = BlockingRunner()
    rt = _runtime(tmp_path, {"echo": EchoRunner(), "block": blocker})
    parent_id, _ = await rt.create_root("echo", {})
    fast_id, _ = await rt.spawn(parent_id, "echo", {}, name="fast")
    slow_id, _ = await rt.spawn(parent_id, "block", {}, name="slow")
    await blocker.started.wait()
    result = await rt.wait_agent(
        [fast_id, slow_id], return_when=ReturnWhen.FIRST_COMPLETED, timeout=5
    )
    assert fast_id in result.completed  # fast 已满足 FIRST_COMPLETED
    assert slow_id not in result.completed
    blocker.release.set()
    await rt.aclose()


@pytest.mark.asyncio
async def test_interrupt_running_run_stops_runner(tmp_path) -> None:
    runner = BlockingRunner()
    rt = _runtime(tmp_path, {"block": runner})
    agent_id, run_id = await rt.create_root("block", {})
    await runner.started.wait()
    await rt.interrupt(agent_id, "stop it")
    assert runner.cancelled
    assert rt.agent_status(agent_id) == AgentStatus.IDLE
    assert rt.run_summary(run_id).status == RunStatus.INTERRUPTED
    await rt.aclose()


@pytest.mark.asyncio
async def test_cancel_run_binds_exact_run_id(tmp_path) -> None:
    runner = BlockingRunner()
    rt = _runtime(tmp_path, {"block": runner})
    agent_id, run_id = await rt.create_root("block", {})
    await runner.started.wait()
    await rt.cancel_run(run_id, reason="caller_cancelled")
    assert rt.run_summary(run_id).status == RunStatus.INTERRUPTED
    runner.release.set()
    await rt.aclose()


@pytest.mark.asyncio
async def test_interrupt_returns_only_after_runner_stopped(tmp_path) -> None:
    runner = SlowUnwindRunner()
    rt = _runtime(tmp_path, {"slow": runner})
    agent_id, run_id = await rt.create_root("slow", {})
    await runner.started.wait()
    await rt.interrupt(agent_id, "stop it")
    assert runner.cancelled  # 无修复时 interrupt 提前返回 → 此处为 False
    assert rt.run_summary(run_id).status == RunStatus.INTERRUPTED
    await rt.aclose()


@pytest.mark.asyncio
async def test_idle_interrupt_raises_not_found(tmp_path) -> None:
    """空闲 Agent 无活跃 turn → interrupt 报 NOT_FOUND（门面语义）。"""
    rt = _runtime(tmp_path, {"echo": EchoRunner()})
    agent_id, run_id = await rt.create_root("echo", {})
    await rt.wait_run(run_id, timeout=5)
    with pytest.raises(AgentCommandError) as raised:
        await rt.interrupt(agent_id, "idle")
    assert raised.value.code == ErrorCode.NOT_FOUND
    await rt.aclose()


@pytest.mark.asyncio
async def test_close_nonrecursive_fails_with_live_descendants(tmp_path) -> None:
    rt = _runtime(tmp_path, {"echo": EchoRunner()})
    parent_id, _ = await rt.create_root("echo", {})
    child_id, _ = await rt.spawn(parent_id, "echo", {}, name="c")
    with pytest.raises(AgentCommandError) as raised:
        await rt.close(parent_id)
    assert raised.value.code == ErrorCode.BUSY
    assert rt.agent_status(parent_id) == AgentStatus.IDLE
    assert rt.agent_status(child_id) is not None
    await rt.aclose()


@pytest.mark.asyncio
async def test_close_recursive_closes_postorder_and_rejects_followup(tmp_path) -> None:
    rt = _runtime(tmp_path, {"echo": EchoRunner()})
    parent_id, _ = await rt.create_root("echo", {})
    child_id, _ = await rt.spawn(parent_id, "echo", {}, name="c")
    await rt.close(parent_id, recursive=True)
    assert rt.agent_status(child_id) == AgentStatus.CLOSED
    assert rt.agent_status(parent_id) == AgentStatus.CLOSED
    with pytest.raises(AgentCommandError) as raised:
        await rt.followup(child_id, {})
    assert raised.value.code == ErrorCode.CLOSED
    await rt.aclose()


@pytest.mark.asyncio
async def test_duplicate_close_is_idempotent(tmp_path) -> None:
    rt = _runtime(tmp_path, {"echo": EchoRunner()})
    agent_id, run_id = await rt.create_root("echo", {})
    await rt.wait_run(run_id, timeout=5)
    await rt.close(agent_id)
    await rt.close(agent_id)
    assert rt.agent_status(agent_id) == AgentStatus.CLOSED
    await rt.aclose()


@pytest.mark.asyncio
async def test_wait_all_completed_partial_timeout_is_timed_out(tmp_path) -> None:
    blocker = BlockingRunner()
    rt = _runtime(tmp_path, {"echo": EchoRunner(), "block": blocker})
    fast_id, _ = await rt.create_root("echo", {})
    slow_id, _ = await rt.create_root("block", {}, name="slow")
    await blocker.started.wait()
    result = await rt.wait_agent(
        [fast_id, slow_id], return_when=ReturnWhen.ALL_COMPLETED, timeout=0.1
    )
    assert result.timed_out is True  # 部分完成仍算超时（B9）
    assert fast_id in result.completed
    assert slow_id not in result.completed
    blocker.release.set()
    await rt.aclose()

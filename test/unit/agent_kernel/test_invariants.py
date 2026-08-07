"""§3.8 验收不变式的端到端验证。"""

import asyncio

import pytest

from athena.core.agent_kernel import __all__ as kernel_exports
from athena.core.agent_kernel.control import AgentControl
from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.types import AgentBusyError, AgentCommandError, AgentSpec

from ._support import BlockingRunner, EchoRunner, JsonCodec, collect_until_terminal


def _spec(runner=None, role="agent") -> AgentSpec:
    return AgentSpec(runner=runner or EchoRunner(), codec=JsonCodec(), role=role)


def _control(**kw) -> AgentControl:
    return AgentControl(AgentKernel(resources_factory=InMemoryResourcesFactory(), **kw))


def test_public_exports_are_exact() -> None:
    assert kernel_exports == [
        "AgentBusyError",
        "AgentCommandError",
        "AgentControl",
        "AgentError",
        "AgentHandle",
        "AgentMessage",
        "AgentRun",
        "AgentRunFailed",
        "AgentRunInterrupted",
        "AgentSnapshot",
        "AgentSpec",
        "AgentStatus",
        "AgentWaitResult",
        "ErrorCode",
        "ForkPolicy",
        "ReturnWhen",
        "RunStatus",
        "RunSummary",
        "TERMINAL_RUN_STATUSES",
    ]


@pytest.mark.asyncio
async def test_invariant_same_agent_never_has_two_nonterminal_runs() -> None:
    control = _control()
    await control.kernel.start()
    runner = BlockingRunner()
    handle, run = await control.create_root(_spec(runner), {})
    await runner.started.wait()
    with pytest.raises(AgentBusyError):
        await control.followup(handle, {})
    runner.release.set()
    await run.wait(timeout=2)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_each_run_has_one_terminal_event() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(_spec(), {})
    await run.wait(timeout=2)
    events = await collect_until_terminal(run.events())
    terminal = [
        e
        for e in events
        if e.kind in ("run_completed", "run_failed", "run_interrupted")
    ]
    assert len(terminal) == 1
    # 事件流为订阅式：终态后短超时探测下一事件，确认无第二个终态事件
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run.events(after_sequence=events[-1].sequence).__anext__(), timeout=0.2
        )
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_busy_followup_changes_nothing() -> None:
    control = _control()
    await control.kernel.start()
    runner = BlockingRunner()
    handle, run = await control.create_root(_spec(runner), {})
    await runner.started.wait()
    runs_before = {rid: r.status for rid, r in control.kernel._store.runs().items()}
    mailbox_before = list(control.kernel._store.mailbox(handle.agent_id))
    with pytest.raises(AgentBusyError):
        await control.followup(handle, {"x": 1})
    assert {
        rid: r.status for rid, r in control.kernel._store.runs().items()
    } == runs_before
    assert control.kernel._store.mailbox(handle.agent_id) == mailbox_before
    runner.release.set()
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_parent_wait_child_no_deadlock_at_one_slot() -> None:
    control = _control(max_active_runs=1)
    await control.kernel.start()

    async def spawn_child(parent_id):
        return await control.kernel.spawn(parent_id, _spec(), {}, name="kid")

    class ParentWaiter:
        def __init__(self):
            self.result = None

        async def run(self, request, *, session, emit):
            _, _ = await spawn_child(session.agent_id)
            self.result = await session.wait_agents(
                [session.agent_id + "/kid"], timeout=5
            )
            return {"ok": True}

    runner = ParentWaiter()
    parent_handle, parent_run = await control.create_root(_spec(runner), {})
    await parent_run.wait(timeout=5)
    assert runner.result is not None
    assert runner.result.timed_out is False  # 子 Run 已完成而非超时
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_closed_agent_rejects_new_messages_runs_and_children() -> None:
    control = _control()
    await control.kernel.start()
    handle, _ = await control.create_root(_spec(), {})
    await control.close(handle)
    with pytest.raises(AgentCommandError):
        await control.send_message(handle, "late")
    with pytest.raises(AgentCommandError):
        await control.followup(handle, {})
    with pytest.raises(AgentCommandError):
        await control.kernel.spawn(handle.agent_id, _spec(), {})
    await control.kernel.aclose()

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.app_server.thread_manager import RuntimeThreadManager
from athena.memory.context_manager import ContextManager
from athena.memory.rollout import RolloutRecorder, resume_context_sync

from ._support import eventually


class _Outcome:
    def __init__(self, ref):
        self.result_ref = ref
        self.next_context_ref = ref


class _MemoryRunner:
    """把触发消息写进 memory 并返回固定 result_ref。"""

    def __init__(self):
        self.refs = []

    async def run_with_context(self, thread, turn, emit, memory, cancel):
        if memory is not None:
            memory.append(ModelRequest(parts=[UserPromptPart(content="echo")]))
        ref = f"art:{len(self.refs)}"
        self.refs.append(ref)
        return _Outcome(ref)


class _CaptureRunner:
    """把每轮 turn 看到的 memory 内容记入 captured,再追加一条 echo。"""

    def __init__(self):
        self.captured: list[list[str]] = []

    async def run_with_context(self, thread, turn, emit, memory, cancel):
        if memory is not None:
            self.captured.append([repr(m) for m in memory.items])
            memory.append(ModelRequest(parts=[UserPromptPart(content="echo")]))
        return _Outcome("art:x")


class _TurnHistoryRunner:
    def __init__(self):
        self.captured: list[list[str]] = []

    async def run_with_context(self, thread, turn, emit, memory, cancel):
        self.captured.append([repr(message) for message in memory.items])
        memory.append(ModelRequest(parts=[UserPromptPart(content=turn.request_ref)]))
        return _Outcome(f"ctx:{turn.turn_id}")


async def test_deterministic_rollout_resumes_context(tmp_path: Path):
    # 必须传 ctx:否则 memory=None,runner 不写 memory,rollout 文件保持空
    manager = RuntimeThreadManager(
        _MemoryRunner(), ctx=ContextManager(), rollout_dir=tmp_path / "sessions"
    )
    thread = await manager.start("agent-1", "ctx1")
    await manager.submit(thread.thread_id, "req1")
    await asyncio.sleep(0.05)  # 让 turn 跑完

    path = tmp_path / "sessions" / "agent-1.jsonl"
    assert path.exists(), f"deterministic rollout missing: {path}"
    assert path.stat().st_size > 0
    ctx = resume_context_sync(path)
    assert any("echo" in repr(m) for m in ctx.items)

    await manager.aclose("done")


async def test_restart_resumes_memory_from_deterministic_rollout(tmp_path: Path):
    # 同一 agent_id 重启复用同一 JSONL 并自动恢复记忆
    rollout_dir = tmp_path / "sessions"
    first = _CaptureRunner()
    manager1 = RuntimeThreadManager(
        first, ctx=ContextManager(), rollout_dir=rollout_dir
    )
    thread1 = await manager1.start("agent-1", "ctx1")
    await manager1.submit(thread1.thread_id, "req1")
    handle1 = await manager1.get(thread1.thread_id)
    await eventually(lambda: handle1.state == "idle")
    await manager1.aclose("done")

    second = _CaptureRunner()
    manager2 = RuntimeThreadManager(
        second, ctx=ContextManager(), rollout_dir=rollout_dir
    )
    thread2 = await manager2.start("agent-1", "ctx1")
    await manager2.submit(thread2.thread_id, "req1")
    handle2 = await manager2.get(thread2.thread_id)
    await eventually(lambda: handle2.state == "idle")
    # 第二程首轮看到恢复的 "echo" — 未恢复则 captured 为空
    assert second.captured, "restarted agent should see restored memory"
    assert any("echo" in line for line in second.captured[0])
    await manager2.aclose("done")


async def test_fork_has_isolated_rollout_at_requested_completed_turn(tmp_path: Path):
    rollout_dir = tmp_path / "sessions"
    runner = _TurnHistoryRunner()
    manager = RuntimeThreadManager(
        runner, ctx=ContextManager(), rollout_dir=rollout_dir
    )
    try:
        parent = await manager.start("agent-1", "ctx0")
        first = await manager.submit(parent.thread_id, "first-prompt")
        parent_handle = await manager.get(parent.thread_id)
        await eventually(lambda: parent_handle.state == "idle")
        second = await manager.submit(parent.thread_id, "second-prompt")
        await eventually(lambda: parent_handle.state == "idle")

        child = await manager.fork(parent.thread_id, after_turn_id=first.turn_id)
        assert child.context_ref == f"ctx:{first.turn_id}"
        await manager.submit(child.thread_id, "child-prompt")
        child_handle = await manager.get(child.thread_id)
        await eventually(lambda: child_handle.state == "idle")

        child_seen = runner.captured[2]
        assert any("first-prompt" in item for item in child_seen)
        assert not any("second-prompt" in item for item in child_seen)
        parent_path = rollout_dir / "agent-1.jsonl"
        child_path = rollout_dir / f"{child.thread_id}.jsonl"
        assert child_path != parent_path
        child_history = resume_context_sync(child_path).items
        assert any("first-prompt" in repr(item) for item in child_history)
        assert not any("second-prompt" in repr(item) for item in child_history)
        assert second.turn_id != first.turn_id
    finally:
        await manager.aclose("done")


async def test_in_memory_fork_preserves_latest_but_rejects_unavailable_old_history():
    runner = _TurnHistoryRunner()
    manager = RuntimeThreadManager(runner, ctx=ContextManager())
    try:
        parent = await manager.start("agent", "ctx0")
        handle = await manager.get(parent.thread_id)
        first = await manager.submit(parent.thread_id, "first-prompt")
        await eventually(lambda: handle.state == "idle")
        latest = await manager.submit(parent.thread_id, "latest-prompt")
        await eventually(lambda: handle.state == "idle")
        child = await manager.fork(parent.thread_id, after_turn_id=latest.turn_id)
        await manager.submit(child.thread_id, "child-prompt")
        child_handle = await manager.get(child.thread_id)
        await eventually(lambda: child_handle.state == "idle")
        assert any("latest-prompt" in item for item in runner.captured[2])
        with pytest.raises(KeyError, match="rollout boundary"):
            await manager.fork(parent.thread_id, after_turn_id=first.turn_id)
    finally:
        await manager.aclose("done")


async def test_turn_boundary_write_failure_preserves_success_and_rejects_missing_fork(
    tmp_path, monkeypatch, caplog
):
    manager = RuntimeThreadManager(
        _TurnHistoryRunner(), ctx=ContextManager(), rollout_dir=tmp_path / "sessions"
    )
    parent = await manager.start("agent", "ctx0")
    handle = await manager.get(parent.thread_id)

    def fail_boundary(self, turn_id):
        raise OSError("disk full")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(RolloutRecorder, "record_turn_boundary", fail_boundary)
            turn = await manager.submit(parent.thread_id, "first-prompt")
            result = await asyncio.wait_for(
                manager.wait_turn(parent.thread_id, turn.turn_id), 1
            )
        assert result == handle.context_ref == f"ctx:{turn.turn_id}"
        assert handle.state == "idle"
        assert not handle._runtime.loop_task.done()
        assert "could not persist historical fork boundary" in caplog.text
        with pytest.raises(KeyError, match="no completed rollout boundary"):
            await asyncio.wait_for(
                manager.fork(parent.thread_id, after_turn_id=turn.turn_id), 1
            )
        latest = await manager.fork(parent.thread_id)
        latest_handle = await manager.get(latest.thread_id)
        assert latest.context_ref == result
        assert "first-prompt" in repr(latest_handle._runtime.memory_snapshot(None))

        # A later successful checkpoint must include the earlier successful messages.
        second = await manager.submit(parent.thread_id, "second-prompt")
        await asyncio.wait_for(manager.wait_turn(parent.thread_id, second.turn_id), 1)
        child = await manager.fork(parent.thread_id, after_turn_id=second.turn_id)
        child_handle = await manager.get(child.thread_id)
        assert child.context_ref == f"ctx:{second.turn_id}"
        assert [
            part.content
            for msg in child_handle._runtime.memory_snapshot(None)
            for part in msg.parts
        ] == ["first-prompt", "second-prompt"]
        assert [
            part.content
            for msg in resume_context_sync(handle._runtime._rollout.path).items
            for part in msg.parts
        ] == ["first-prompt", "second-prompt"]
        assert not handle._runtime.loop_task.done()
    finally:
        await manager.aclose("done")

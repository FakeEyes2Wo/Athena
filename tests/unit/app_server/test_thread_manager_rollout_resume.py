import asyncio
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.app_server.thread_manager import RuntimeThreadManager
from athena.memory.context_manager import ContextManager
from athena.memory.rollout import resume_context_sync

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

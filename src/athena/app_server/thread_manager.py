"""ThreadManager 实现 — 进程级 Thread 注册表 + ThreadRuntime 工厂。

实现 ``execution.thread_manager.ThreadManager`` ABC，
内部使用 ThreadRuntime/ThreadHandle/SubmissionLoop 架构。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from athena.app_server.submissions import GetForkSnapshot, InterruptTurn, StartTurn
from athena.app_server.thread_runtime import Runner, ThreadHandle, ThreadRuntime
from athena.core.schemas import ArtifactRef, AthenaThread, AthenaTurn
from athena.execution.handlers import ThreadManager as ThreadManagerABC
from athena.memory.context_manager import ContextManager
from athena.memory.rollout import RolloutRecorder

logger = logging.getLogger(__name__)


class RuntimeThreadManager(ThreadManagerABC):
    """基于 ThreadRuntime 的 ThreadManager 实现。对齐 Codex ``ThreadManager``。

    Args:
        runner: Turn 执行函数。
        ctx: 可选的 ContextManager — 对话上下文管理。
        compactor: 可选的 Compactor — 上下文压缩。
        rollout: 可选的 RolloutRecorder — JSONL 持久化。
        llm: 可选的 LLM 客户端 — compaction 需要。
    """

    def __init__(
        self,
        runner: Runner,
        *,
        ctx: Any = None,
        compactor: Any = None,
        rollout: Any = None,
        llm: Any = None,
        project_root: Path | None = None,
    ) -> None:
        if not callable(runner):
            raise TypeError("runner must be callable")
        self._runner = runner
        self._lock = asyncio.Lock()
        self._handles: dict[str, ThreadHandle] = {}
        self._state: str = "alive"
        self._close_future: asyncio.Future[None] = asyncio.Future()
        self._memory_kwargs = {
            "ctx": ctx,
            "compactor": compactor,
            "rollout": rollout,
            "llm": llm,
        }
        self._project_root = project_root or Path.cwd()

    def _make_runtime(
        self, thread_id: str, session_id: str, context_ref: str
    ) -> ThreadRuntime:
        """每个 Thread 创建独立的 Memory 组件 — 防止串话。"""
        kwargs: dict[str, Any] = {}
        if self._memory_kwargs["ctx"] is not None:
            kwargs["ctx"] = ContextManager(
                context_limit=self._memory_kwargs["ctx"].limit
            )
        if self._memory_kwargs["compactor"] is not None:
            kwargs["compactor"] = self._memory_kwargs["compactor"]
        if self._memory_kwargs["rollout"] is not None:
            kwargs["rollout"] = RolloutRecorder(self._project_root)
        if self._memory_kwargs["llm"] is not None:
            kwargs["llm"] = self._memory_kwargs["llm"]
        return ThreadRuntime(
            thread_id=thread_id,
            session_id=session_id,
            context_ref=context_ref,
            runner=self._runner,
            **kwargs,
        )

    async def start(self, session_id: str, context_ref: ArtifactRef) -> AthenaThread:
        self._require_ref(session_id, "session_id")
        self._require_ref(context_ref, "context_ref")
        thread_id = str(uuid4())
        # 先检查状态 — 避免创建 runtime 后因状态不对而泄漏后台任务
        async with self._lock:
            if self._state != "alive":
                raise RuntimeError("manager is not alive")
        runtime = self._make_runtime(thread_id, session_id, context_ref)
        await runtime.start()
        handle = ThreadHandle(runtime)
        async with self._lock:
            if self._state != "alive":
                await runtime.shutdown("manager closed")
                raise RuntimeError("manager is not alive")
            self._handles[thread_id] = handle
        return AthenaThread(
            thread_id=thread_id,
            session_id=session_id,
            status="idle",
            context_ref=context_ref,
        )

    async def submit(self, thread_id: str, request_ref: ArtifactRef) -> AthenaTurn:
        self._require_ref(thread_id, "thread_id")
        self._require_ref(request_ref, "request_ref")
        handle = await self.get(thread_id)
        turn_id = str(uuid4())
        await handle.submit(StartTurn(turn_id=turn_id, request_ref=request_ref))
        return AthenaTurn(
            turn_id=turn_id,
            thread_id=thread_id,
            request_ref=request_ref,
            status="running",
        )

    async def fork(
        self, thread_id: str, after_turn_id: str | None = None
    ) -> AthenaThread:
        self._require_ref(thread_id, "thread_id")
        async with self._lock:
            if self._state != "alive":
                raise RuntimeError("manager is not alive")
            parent = self._handles.get(thread_id)
            if parent is None:
                raise KeyError(f"unknown thread: {thread_id}")
            context_ref = await parent.submit(
                GetForkSnapshot(after_turn_id=after_turn_id)
            )
            session_id = parent._runtime.session_id
            child_id = str(uuid4())
            child_runtime = self._make_runtime(child_id, session_id, context_ref)
            await child_runtime.start()
            self._handles[child_id] = ThreadHandle(child_runtime)
        return AthenaThread(
            thread_id=child_id,
            session_id=session_id,
            status="idle",
            context_ref=context_ref,
        )

    async def interrupt(self, thread_id: str, turn_id: str, reason: str) -> None:
        self._require_ref(thread_id, "thread_id")
        self._require_ref(turn_id, "turn_id")
        self._require_ref(reason, "reason")
        handle = await self.get(thread_id)
        await handle.submit(InterruptTurn(turn_id=turn_id, reason=reason))

    def events(self, thread_id: str) -> AsyncIterator[ArtifactRef]:
        self._require_ref(thread_id, "thread_id")

        async def _iter() -> AsyncIterator[ArtifactRef]:
            handle = await self.get(thread_id)
            async for event in handle.events():
                yield event.event_ref

        return _iter()

    async def get(self, thread_id: str) -> ThreadHandle:
        async with self._lock:
            if thread_id not in self._handles:
                raise KeyError(f"unknown thread: {thread_id}")
            return self._handles[thread_id]

    async def get_thread(self, thread_id: str) -> AthenaThread:
        handle = await self.get(thread_id)
        r = handle._runtime
        return AthenaThread(
            thread_id=r.thread_id,
            session_id=r.session_id,
            status=r.state,
            context_ref=r.context_ref,
        )

    async def aclose(self, reason: str = "server_shutdown") -> None:
        should_shutdown = False
        async with self._lock:
            if self._state != "alive":
                # 不持锁等待 — 避免与执行关闭的线程死锁
                pass
            else:
                self._state = "closing"
                handles = list(self._handles.values())
                should_shutdown = True
        if not should_shutdown:
            await self._close_future
            return
        results = await asyncio.gather(
            *(h.shutdown_and_wait(reason) for h in handles), return_exceptions=True
        )
        for exc in results:
            if isinstance(exc, Exception):
                logger.warning("error during thread shutdown: %s", exc)
        async with self._lock:
            self._state = "closed"
            self._handles.clear()
            if not self._close_future.done():
                self._close_future.set_result(None)

    @staticmethod
    def _require_ref(value: object, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")

    @property
    def state(self) -> str:
        return self._state

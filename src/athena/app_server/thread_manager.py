"""进程级 Thread 注册表与 ThreadRuntime 工厂。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from athena.app_server.submissions import GetForkSnapshot, InterruptTurn, StartTurn
from athena.app_server.thread_runtime import Runner, ThreadHandle, ThreadRuntime
from athena.core.contracts import ArtifactRef
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.execution import MonitorLimits
from athena.memory.context_manager import ContextManager
from athena.memory.rollout import RolloutRecorder, resume_context_sync

logger = logging.getLogger(__name__)


class RuntimeThreadManager:
    """基于 ThreadRuntime 的 ThreadManager 实现。对齐 Codex ``ThreadManager``。

    Args:
        runner: Turn 执行函数。
        ctx: 可选的 ContextManager — 对话上下文管理。
        compactor: 可选的 Compactor — 上下文压缩。
        rollout: 可选的 RolloutRecorder — JSONL 持久化。
        llm: 可选的 LLM 客户端 — compaction 需要。
        rollout_dir: 可选的确定性 rollout 目录 — 每 agent(session_id)一个
            ``{rollout_dir}/{agent_id}.jsonl``;文件非空则启动时自动恢复记忆。
        on_turn_terminal: 可选的终态回调 — 透传每个 ThreadRuntime。
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
        monitor_limits: MonitorLimits | None = None,
        monitor_scan_interval: float = 1.0,
        monitor_terminal_retention: float = 300.0,
        rollout_dir: Path | None = None,
        on_turn_terminal: Any | None = None,
    ) -> None:
        if not callable(runner) and not hasattr(runner, "run_with_context"):
            raise TypeError("runner must be callable or expose run_with_context")
        self._runner = runner
        self._lock = asyncio.Lock()
        self._handles: dict[str, ThreadHandle] = {}
        self._state: str = "alive"
        self._close_future: asyncio.Future[None] | None = None
        self._memory_kwargs = {
            "ctx": ctx,
            "compactor": compactor,
            "rollout": rollout,
            "llm": llm,
        }
        self._project_root = project_root or Path.cwd()
        self._rollout_dir = Path(rollout_dir) if rollout_dir is not None else None
        self._on_turn_terminal = on_turn_terminal
        self._monitor_kwargs = {
            "monitor_limits": monitor_limits,
            "monitor_scan_interval": monitor_scan_interval,
            "monitor_terminal_retention": monitor_terminal_retention,
        }

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
        if self._memory_kwargs["rollout"] is not None or self._rollout_dir is not None:
            recorder = RolloutRecorder(self._project_root)
            if self._rollout_dir is not None:
                # 确定性路径:同一 agent_id(session_id)重启复用同一 JSONL
                path = self._rollout_dir / f"{session_id}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                recorder.open_sync(session_id, append_to=path)
                if path.exists() and path.stat().st_size > 0:
                    # COMPAT: 重开会话恢复记忆;清理条件:Codex 风格会话恢复并入 ThreadRuntime 后。
                    kwargs["ctx"] = resume_context_sync(path)
            kwargs["rollout"] = recorder
        if self._memory_kwargs["llm"] is not None:
            kwargs["llm"] = self._memory_kwargs["llm"]
        return ThreadRuntime(
            thread_id=thread_id,
            session_id=session_id,
            context_ref=context_ref,
            runner=self._runner,
            on_turn_terminal=self._on_turn_terminal,
            **self._monitor_kwargs,
            **kwargs,
        )

    async def start(
        self, session_id: str, context_ref: ArtifactRef, *, thread_id: str | None = None
    ) -> AthenaThread:
        """创建新 Thread 并启动其 runtime。

        thread_id 缺省为随机值;显式传入可使 thread_id == session_id
        (AgentRuntime 门面按 agent_id 寻址 Thread)。
        """
        self._require_ref(session_id, "session_id")
        self._require_ref(context_ref, "context_ref")
        if self._rollout_dir is not None:
            self.require_safe_path_basename(session_id, "session_id")
        thread_id = thread_id or str(uuid4())
        self._require_ref(thread_id, "thread_id")
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
        """向指定 Thread 提交新 Turn。"""
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
        """从指定 Turn 之后 fork 一个新 Thread，共享上下文快照。"""
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
        """中断指定 Thread 上正在运行的 Turn。"""
        self._require_ref(thread_id, "thread_id")
        self._require_ref(turn_id, "turn_id")
        self._require_ref(reason, "reason")
        handle = await self.get(thread_id)
        await handle.submit(InterruptTurn(turn_id=turn_id, reason=reason))

    async def wait_turn(self, thread_id: str, turn_id: str) -> ArtifactRef:
        self._require_ref(thread_id, "thread_id")
        self._require_ref(turn_id, "turn_id")
        return await (await self.get(thread_id)).wait_turn(turn_id)

    def events(self, thread_id: str) -> AsyncIterator[ArtifactRef]:
        """返回指定 Thread 的事件流迭代器。"""
        self._require_ref(thread_id, "thread_id")

        async def _iter() -> AsyncIterator[ArtifactRef]:
            handle = await self.get(thread_id)
            async for event in handle.events():
                yield event.event_ref

        return _iter()

    async def get(self, thread_id: str) -> ThreadHandle:
        """获取指定 Thread 的句柄。不存在则抛出 KeyError。"""
        async with self._lock:
            if thread_id not in self._handles:
                raise KeyError(f"unknown thread: {thread_id}")
            return self._handles[thread_id]

    async def get_thread(self, thread_id: str) -> AthenaThread:
        """返回 Thread 的元数据快照。"""
        handle = await self.get(thread_id)
        r = handle._runtime
        return AthenaThread(
            thread_id=r.thread_id,
            session_id=r.session_id,
            status=r.state,
            context_ref=r.context_ref,
        )

    async def aclose(self, reason: str = "server_shutdown") -> None:
        """关闭所有 Thread 并清理资源。"""
        should_shutdown = False
        async with self._lock:
            if self._close_future is None:
                self._close_future = asyncio.get_running_loop().create_future()
            if self._state != "alive":
                # 不持锁等待 — 避免与执行关闭的线程死锁
                pass
            else:
                self._state = "closing"
                handles = list(self._handles.values())
                should_shutdown = True
        if not should_shutdown:
            assert self._close_future is not None
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
            assert self._close_future is not None
            if not self._close_future.done():
                self._close_future.set_result(None)

    @staticmethod
    def _require_ref(value: object, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")

    @staticmethod
    def require_safe_path_basename(value: object, name: str) -> str:
        RuntimeThreadManager._require_ref(value, name)
        assert isinstance(value, str)
        stem = value.split(".", 1)[0].upper()
        reserved_stems = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        if (
            value in {".", ".."}
            or Path(value).name != value
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in value)
            or value.endswith((".", " "))
            or stem in reserved_stems
        ):
            raise ValueError(f"{name} must be a safe path basename")
        return value

    @property
    def state(self) -> str:
        return self._state

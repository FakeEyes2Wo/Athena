"""AgentSession — memory、mailbox 游标与事件 journal（设计 §1.2、§3.3）。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from athena.core.agent_kernel.store import AgentGraphStore
from athena.core.agent_kernel.types import (
    AgentEvent,
    AgentId,
    AgentMessage,
    AgentSpec,
    ArtifactRef,
    RunId,
)
from athena.memory.context_manager import ContextManager
from athena.memory.rollout import RolloutRecorder, resume_context_sync


class SessionResources:
    """单个 Session 的资源绑定。工厂注入，不由 Kernel 硬编码。"""

    def __init__(
        self,
        *,
        memory: ContextManager | None = None,
        recorder: RolloutRecorder | None = None,
    ) -> None:
        self._memory = memory or ContextManager()
        self._recorder = recorder

    @property
    def memory(self) -> ContextManager:
        return self._memory

    @property
    def recorder(self) -> RolloutRecorder | None:
        """可选的 rollout 记录器；None 表示不持久化 memory。"""
        return self._recorder

    @property
    def context_ref(self) -> str | None:
        """rollout 文件路径；无记录器返回 None。"""
        path = self._recorder.path if self._recorder is not None else None
        return str(path) if path is not None else None


class SessionResourcesFactory(Protocol):
    """为每个 Agent Session 构造独立资源的工厂。"""

    def create(
        self,
        agent_id: AgentId,
        spec: AgentSpec,
        *,
        context_ref: str | None = None,
    ) -> SessionResources: ...
    async def aclose(self) -> None: ...


class InMemoryResourcesFactory:
    """默认工厂：每个 Session 一个独立 ContextManager（仅用于测试）。"""

    def create(
        self,
        agent_id: AgentId,
        spec: AgentSpec,
        *,
        context_ref: str | None = None,
    ) -> SessionResources:
        del agent_id, spec, context_ref
        return SessionResources()

    async def aclose(self) -> None:
        """释放全部资源。内存实现无外部句柄，钩子保留给持久化资源。"""


class RolloutResourcesFactory:
    """生产工厂：按 context_ref 恢复私有记忆，turn 内追加到同一 rollout（设计 §9）。

    每个 Agent 一个稳定 rollout 文件作为其 ``context_ref``；创建时新建文件，
    恢复时 ``append_to`` 复用同一文件，保证多次重启后记忆持续累积。
    """

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root)
        self._recorders: list[RolloutRecorder] = []

    def create(
        self,
        agent_id: AgentId,
        spec: AgentSpec,
        *,
        context_ref: str | None = None,
    ) -> SessionResources:
        del spec
        memory = (
            resume_context_sync(Path(context_ref)) if context_ref is not None else None
        )
        recorder = RolloutRecorder(self._root)
        append_to = Path(context_ref) if context_ref is not None else None
        recorder.open_sync(agent_id, append_to=append_to)
        self._recorders.append(recorder)
        return SessionResources(memory=memory, recorder=recorder)

    async def aclose(self) -> None:
        """关闭全部 rollout 记录器。"""
        for recorder in self._recorders:
            await recorder.close()
        self._recorders.clear()


class _MemoryView:
    """ContextManager 的只读视图：读/回滚可用，写入必须经 session 门禁（B8）。

    ``allow_rollback=False`` 用于 runner 视图：rollback 是 kernel 内部操作（B5）。
    """

    def __init__(self, memory: ContextManager, *, allow_rollback: bool = True) -> None:
        self._memory = memory
        self._allow_rollback = allow_rollback

    @property
    def raw(self) -> ContextManager:
        """底层 ContextManager（供 BaseAgent 适配器构造 AgentContext）。"""
        return self._memory

    @property
    def items(self) -> list[ModelMessage]:
        return self._memory.items

    @property
    def tokens(self) -> int:
        return self._memory.tokens

    @property
    def version(self) -> int:
        return self._memory.version

    @property
    def limit(self) -> int:
        return self._memory.limit

    def token_margin(self, ratio: float = 0.85) -> int:
        return self._memory.token_margin(ratio)

    def snapshot(self) -> tuple[int, int]:
        return self._memory.snapshot()

    def items_since(self, idx: int) -> list[ModelMessage]:
        return self._memory.items_since(idx)

    def rollback(self, idx: int) -> None:
        if not self._allow_rollback:
            raise AttributeError("rollback is kernel-internal")
        self._memory.rollback(idx)

    def append(self, msg: ModelMessage) -> None:
        raise AttributeError("memory writes must go through session.append_message")

    def replace_range(self, *args: Any, **kwargs: Any) -> None:
        raise AttributeError("memory writes must go through session.append_message")


class AgentSession:
    """单个 Agent 的会话态：memory、mailbox 游标、journal、interrupt。"""

    def __init__(
        self,
        *,
        agent_id: AgentId,
        spec: AgentSpec,
        resources: SessionResources,
        store: AgentGraphStore,
        kernel: Any = None,
    ) -> None:
        self._agent_id = agent_id
        self._spec = spec
        self._resources = resources
        self._store = store
        self._kernel = kernel
        self._events: list[AgentEvent] = []
        self._next_event_sequence = 1
        self._wakeup = asyncio.Event()
        self._active_run_id: RunId | None = None
        self._active_generation: int | None = None
        # 可见游标按 (run_id, generation) 隔离：旧 Run 不推进新 Run 的可见状态（R4）
        self._visible: dict[tuple[RunId | None, int | None], int] = {}
        self._memory_view = _MemoryView(self._resources.memory)
        self._closed = False

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def memory(self) -> _MemoryView:
        """只读 memory 视图；写入必须经 ``append_message``（带 run/generation 门禁，B8）。"""
        return self._memory_view

    @property
    def context_ref(self) -> str:
        """私有记忆的持久化引用；无 rollout 记录器时退回合成标识。"""
        return self._resources.context_ref or f"context://{self._agent_id}"

    @property
    def active_run_id(self) -> RunId | None:
        return self._active_run_id

    def set_active_run(self, run_id: RunId, generation: int) -> None:
        """标记当前运行 Run 及其 generation；其事件与 memory 写入生效。"""
        self._active_run_id = run_id
        self._active_generation = generation

    def clear_active_run(self) -> None:
        self._active_run_id = None
        self._active_generation = None

    def interrupt(self) -> None:
        """中断后旧 runner 不可再写 memory 或事件（§3.5）。"""
        self._active_run_id = None
        self._active_generation = None

    def close(self) -> None:
        self._closed = True
        self._active_run_id = None
        self._active_generation = None

    def receive_messages(
        self, *, run_id: RunId | None = None, generation: int | None = None
    ) -> list[AgentMessage]:
        """返回未读 mailbox 窗口并推进该 Run 的可见游标；重复读取不再重投（B4）。

        可见游标按 (run_id, generation) 隔离；旧 Run 视图不得读取或推进（R4）。
        """
        if run_id is not None and self._active_run_id != run_id:
            return []  # 旧 Run 视图 → 不读取
        if generation is not None and self._active_generation != generation:
            return []
        committed = self._store.mailbox_committed(self._agent_id)
        messages = self._store.mailbox(self._agent_id)
        key = (run_id, generation)
        visible = max(self._visible.get(key, 0), committed)  # 单调，不回退
        window = list(messages[visible:])
        self._visible[key] = len(messages)
        return window

    def checkpoint(
        self, *, run_id: RunId | None = None, generation: int | None = None
    ) -> None:
        """推进 committed_cursor 到已读位置；仅当前 Run 且同 generation 可提交（B7）。

        无活跃 Run（已被中断/关闭）或 run/generation 不匹配 → 拒绝，旧 runner 不可提交。
        """
        if self._active_run_id is None:
            return
        if run_id is not None and self._active_run_id != run_id:
            return
        if generation is not None and self._active_generation != generation:
            return
        key = (run_id, generation)
        visible = self._visible.get(key, self._store.mailbox_committed(self._agent_id))
        self._store.commit(
            command_id=f"ckpt:{self._agent_id}:{self._store.sequence + 1}",
            kind="mailbox_committed",
            payload={"agent_id": self._agent_id, "committed": visible},
        )

    def append_message(
        self, run_id: RunId, generation: int, message: ModelMessage
    ) -> None:
        """受门禁的 memory 写入；仅当前 Run 且同 generation 可写，同步追加 rollout。"""
        if (
            self._closed
            or self._active_run_id != run_id
            or self._active_generation != generation
        ):
            return
        self._resources.memory.append(message)
        recorder = self._resources.recorder
        if recorder is not None:
            recorder.record(message)

    def _next_sequence(self) -> int:
        seq = self._next_event_sequence
        self._next_event_sequence += 1
        return seq

    async def append_event(
        self,
        run_id: RunId,
        generation: int,
        kind: str,
        event_ref: ArtifactRef,
        data: dict[str, Any] | None = None,
    ) -> None:
        """追加当前 Run 事件；非当前 Run/generation 或已关闭的事件丢弃（B8）。

        事件同时经序列器耐久写入 Store（R3）；持久化失败仅记日志，不阻断流式发布。
        """
        if (
            self._closed
            or self._active_run_id != run_id
            or self._active_generation != generation
        ):
            return
        event = AgentEvent(run_id, self._next_sequence(), kind, event_ref, data)
        self._events.append(event)
        self._wakeup.set()
        # 持久化为 fire-and-forget：入队顺序保证与终态事务一致，且不阻塞流式发布（R3）
        if self._kernel is not None:
            asyncio.create_task(self._kernel.persist_event(self._agent_id, event))

    def append_terminal_event(
        self,
        run_id: RunId,
        kind: str,
        event_ref: ArtifactRef,
        data: dict[str, Any] | None = None,
    ) -> None:
        """追加终态事件；由 Kernel 在终态提交时调用（有意不受门禁，并唤醒订阅者）。"""
        event = AgentEvent(run_id, self._next_sequence(), kind, event_ref, data)
        self._events.append(event)
        self._wakeup.set()

    def build_terminal_event(
        self,
        run_id: RunId,
        kind: str,
        event_ref: ArtifactRef,
        data: dict[str, Any] | None = None,
    ) -> AgentEvent:
        """构造终态事件（推进序列）；发布时机由调用方决定（先持久后发布，B1/R3）。"""
        return AgentEvent(run_id, self._next_sequence(), kind, event_ref, data)

    def publish_event(self, event: AgentEvent) -> None:
        """向订阅者发布事件（live journal 追加 + 唤醒）。"""
        self._events.append(event)
        self._wakeup.set()

    def seed_events(self, events: list[AgentEvent]) -> None:
        """恢复时以 Store 持久化的事件填充 live journal（R3）。"""
        self._events = list(events)
        if events:
            self._next_event_sequence = max(e.sequence for e in events) + 1

    def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        """返回整条 Session journal（跨 Run，订阅式：持续等待新事件，由调用方决定何时终止读取，§5.1）。"""
        return self._event_iterator(after_sequence, run_id=None)

    def run_events(
        self, run_id: RunId, after_sequence: int = 0
    ) -> AsyncIterator[AgentEvent]:
        """返回指定 Run 的事件流（订阅式），供 run.events()。"""
        return self._event_iterator(after_sequence, run_id=run_id)

    async def _event_iterator(
        self, after_sequence: int, run_id: RunId | None
    ) -> AsyncIterator[AgentEvent]:
        index = max(0, after_sequence)
        while True:
            while index >= len(self._events):
                self._wakeup.clear()
                if index < len(self._events):
                    break
                await self._wakeup.wait()
            event = self._events[index]
            index += 1
            if run_id is None or event.run_id == run_id:
                yield event

    async def wait_agents(
        self, target_ids: list[AgentId], *, timeout: float | None = None
    ) -> Any:
        """由运行中的 runner 调用：等待目标终结，期间释放本 Run 的 lease（§3.6）。"""
        if self._kernel is None:
            raise RuntimeError("session has no kernel binding")
        return await self._kernel.wait_agent_parked(
            target_ids,
            parking_run_id=self._active_run_id,
            parking_generation=self._active_generation,
            timeout=timeout,
        )


class RunSession:
    """绑定单个 Run 的受限 session 视图（B5）：方法自动校验 run/generation。

    runner 只能拿到本视图；旧 runner 借新 Run 活跃态提交旧游标或回滚新 Run
    memory 均被拒绝。memory 为无 rollback 的只读视图。
    """

    def __init__(self, session: AgentSession, run_id: RunId, generation: int) -> None:
        self._session = session
        self._run_id = run_id
        self._generation = generation
        self._memory = _MemoryView(session._resources.memory, allow_rollback=False)

    @property
    def agent_id(self) -> AgentId:
        return self._session.agent_id

    @property
    def kernel(self) -> Any:
        """当前 Run 绑定的 Kernel（供受控编排工具使用）。"""
        kernel = self._session._kernel
        if kernel is None:
            raise RuntimeError("session has no kernel binding")
        return kernel

    @property
    def context_ref(self) -> str:
        return self._session.context_ref

    @property
    def memory(self) -> _MemoryView:
        """只读 memory 视图（无 rollback）；写入经 ``append_message`` 门禁。"""
        return self._memory

    def receive_messages(self) -> list[AgentMessage]:
        """返回本 Run 的未读窗口，游标归属于本 (run, generation)（R4）。"""
        return self._session.receive_messages(
            run_id=self._run_id, generation=self._generation
        )

    def checkpoint(self) -> None:
        """推进 committed_cursor；仅当本 Run 仍是活跃 Run 时生效（B5）。"""
        self._session.checkpoint(run_id=self._run_id, generation=self._generation)

    def append_message(self, message: ModelMessage) -> None:
        self._session.append_message(self._run_id, self._generation, message)

    async def append_event(
        self, kind: str, event_ref: ArtifactRef, data: dict[str, Any] | None = None
    ) -> None:
        await self._session.append_event(
            self._run_id, self._generation, kind, event_ref, data
        )

    async def wait_agents(
        self, target_ids: list[AgentId], *, timeout: float | None = None
    ) -> Any:
        """等待目标终结；park 用本视图固有的 run/generation，旧视图不得 park 新 Run（R5）。"""
        kernel = self._session._kernel
        if kernel is None:
            raise RuntimeError("session has no kernel binding")
        return await kernel.wait_agent_parked(
            target_ids,
            parking_run_id=self._run_id,
            parking_generation=self._generation,
            timeout=timeout,
        )

    async def wait_for_human(
        self, content: str, context_refs: list[str] | None = None
    ) -> str:
        """持久化登记人工等待并结束本 turn（设计 §7.2）。返回稳定 request id。"""
        kernel = self._session._kernel
        if kernel is None:
            raise RuntimeError("session has no kernel binding")
        return await kernel.wait_for_human(
            self._session.agent_id, content, context_refs
        )

    async def wait_for(self, target_ids: list[AgentId]) -> None:
        """持久化登记对目标 Agent 的依赖等待并结束本 turn（设计 §7.2）。"""
        kernel = self._session._kernel
        if kernel is None:
            raise RuntimeError("session has no kernel binding")
        await kernel.wait_for(self._session.agent_id, target_ids)

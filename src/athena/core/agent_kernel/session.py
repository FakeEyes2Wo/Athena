"""AgentSession — memory、mailbox 游标与事件 journal（设计 §1.2、§3.3）。"""

import asyncio
from collections.abc import AsyncIterator
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


class SessionResources:
    """单个 Session 的资源绑定。工厂注入，不由 Kernel 硬编码。"""

    def __init__(self, *, memory: ContextManager | None = None) -> None:
        self._memory = memory or ContextManager()

    @property
    def memory(self) -> ContextManager:
        return self._memory


class SessionResourcesFactory(Protocol):
    """为每个 Agent Session 构造独立资源的工厂。"""

    def create(self, agent_id: AgentId, spec: AgentSpec) -> SessionResources: ...
    async def aclose(self) -> None: ...


class InMemoryResourcesFactory:
    """默认工厂：每个 Session 一个独立 ContextManager。"""

    def create(self, agent_id: AgentId, spec: AgentSpec) -> SessionResources:
        del agent_id, spec
        return SessionResources()

    async def aclose(self) -> None:
        """释放全部资源。内存实现无外部句柄，钩子保留给持久化资源。"""


class _MemoryView:
    """ContextManager 的只读视图：读/回滚可用，写入必须经 session 门禁（B8）。"""

    def __init__(self, memory: ContextManager) -> None:
        self._memory = memory

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
        self._visible = 0
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
        return f"context://{self._agent_id}"

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

    def receive_messages(self) -> list[AgentMessage]:
        """返回未提交（[committed, len)）的 mailbox 窗口，并推进可见游标。"""
        committed = self._store.mailbox_committed(self._agent_id)
        messages = self._store.mailbox(self._agent_id)
        self._visible = max(
            self._visible, committed, len(messages)
        )  # 单调，不回退（B7）
        return list(messages[committed:])

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
        self._store.commit(
            command_id=f"ckpt:{self._agent_id}:{self._store.sequence + 1}",
            kind="mailbox_committed",
            payload={"agent_id": self._agent_id, "committed": self._visible},
        )

    def append_message(
        self, run_id: RunId, generation: int, message: ModelMessage
    ) -> None:
        """受门禁的 memory 写入；仅当前 Run 且同 generation 可写。"""
        if (
            self._closed
            or self._active_run_id != run_id
            or self._active_generation != generation
        ):
            return
        self._resources.memory.append(message)

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
        """追加当前 Run 事件；非当前 Run/generation 或已关闭的事件丢弃（B8）。"""
        if (
            self._closed
            or self._active_run_id != run_id
            or self._active_generation != generation
        ):
            return
        event = AgentEvent(run_id, self._next_sequence(), kind, event_ref, data)
        self._events.append(event)
        self._wakeup.set()

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
            target_ids, parking_run_id=self._active_run_id, timeout=timeout
        )

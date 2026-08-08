"""事件系统 — Event 模型、EventJournal、Subscription、FairMux。

每 Thread 一个 EventJournal 作为权威事件顺序源；
FairMux 将多个订阅的事件多路复用到单一输出流。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from athena.app_server.protocol import EventNotification
from athena.core.contracts import ArtifactRef

logger = logging.getLogger(__name__)


class Event(BaseModel):
    """每 Thread 权威事件记录 — 支持内联数据。

    文本 delta → data={"delta": "..."} 内联。
    大型结果 → artifact_ref 指向 ArtifactStore。
    """

    thread_id: str
    turn_id: str | None = None
    sequence: int = Field(ge=1)
    kind: str
    event_ref: ArtifactRef = ""
    data: dict[str, Any] | None = None
    """内联小型数据（text delta、function_call 参数等）。"""
    model_config = {"frozen": True}


class EventJournal:
    """每 Thread 一个。只追加，用 ``asyncio.Condition`` 同时做互斥和通知。"""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self._records: list[Event] = []
        self._condition = asyncio.Condition()
        self._next_sequence = 1

    @property
    def condition(self) -> asyncio.Condition:
        """事件读写共享的条件变量（配合 append/read_from 使用）。"""
        return self._condition

    def next_sequence(self) -> int:
        """下一条事件的序列号（= 已追加数量）。"""
        return self._next_sequence

    def append(self, event: Event) -> None:
        """追加事件并通知。调用者必须持有 ``condition`` 锁。"""
        self._records.append(event)
        self._next_sequence = event.sequence + 1
        self._condition.notify_all()

    def read_from(self, after_sequence: int = 0) -> AsyncIterator[Event]:
        """返回从指定 sequence 之后开始的异步事件迭代器。"""
        return self._event_iterator(after_sequence)

    async def _event_iterator(self, after_sequence: int) -> AsyncIterator[Event]:
        index = max(0, after_sequence)
        while True:
            async with self._condition:
                await self._condition.wait_for(lambda: index < len(self._records))
                event = self._records[index]
                index += 1
            yield event

    def __len__(self) -> int:
        return len(self._records)

    @property
    def last_sequence(self) -> int:
        """最后一条事件的序列号；无事件返回 -1。"""
        return self._next_sequence - 1


@dataclass(slots=True)
class Subscription:
    """事件订阅——追踪 cursor、缓冲队列和活跃状态。"""

    subscription_id: str
    thread_id: str
    cursor: int = 0
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(64))
    pump_task: asyncio.Task[None] | None = None
    active: bool = True
    has_data: asyncio.Event = field(default_factory=asyncio.Event)


class FairMux:
    """Event 驱动的多订阅轮询器——round-robin 防饥饿，Event 替代轮询。"""

    def __init__(self, send_event) -> None:
        self._send = send_event
        self._subs: dict[str, Subscription] = {}
        self._task: asyncio.Task[None] | None = None
        self._rr_index = 0
        self._has_subscriptions = asyncio.Event()
        self._wake = asyncio.Event()  # add() 时 set，唤醒 _run 检测新订阅

    def add(self, sub: Subscription) -> None:
        """添加订阅并唤醒轮询循环。"""
        self._subs[sub.subscription_id] = sub
        self._has_subscriptions.set()
        self._wake.set()  # 唤醒 _run 以检测新订阅的数据

    def remove(self, subscription_id: str) -> None:
        """移除订阅；无订阅时清除标记信号量。"""
        self._subs.pop(subscription_id, None)
        if not self._subs:
            self._has_subscriptions.clear()

    async def start(self) -> None:
        """启动 FairMux 后台轮询任务。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="fair-mux")

    async def stop(self) -> None:
        """取消 FairMux 后台任务并等待退出。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # mux task 被 cancel → 等待完毕，预期行为
                pass
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                ready = [
                    s for s in self._subs.values() if s.active and not s.queue.empty()
                ]
                if not ready:
                    tasks = [
                        asyncio.ensure_future(s.has_data.wait())
                        for s in self._subs.values()
                        if s.active
                    ]
                    if not tasks:
                        await self._has_subscriptions.wait()
                        continue
                    # 同时等待新订阅 — add() 中 _wake.set() 会唤醒此等待
                    tasks.append(asyncio.ensure_future(self._wake.wait()))
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for t in tasks:
                            t.cancel()
                    self._wake.clear()
                    continue

                # round-robin：从上一次的下一个位置开始
                self._rr_index = self._rr_index % len(ready)
                sub = ready[self._rr_index]
                self._rr_index += 1

                try:
                    event = sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    # 队列被另一个消费者抢先读空 → 清除标记，继续轮询
                    sub.has_data.clear()
                    continue
                await self._send(
                    EventNotification(
                        subscription_id=sub.subscription_id,
                        thread_id=event.thread_id,
                        turn_id=event.turn_id,
                        sequence=event.sequence,
                        kind=event.kind,
                        event_ref=event.event_ref,
                        data=event.data,
                    )
                )
                if sub.queue.empty():
                    sub.has_data.clear()
        except asyncio.CancelledError:
            # FairMux 被 stop() 取消 → 正常退出
            pass

"""生命周期编排 — 启动 / 优雅关闭。

使用 ``contextlib.AsyncExitStack`` 管理资源清理——保证逆序、不漏。
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from uuid import uuid4

from athena.app_server.client import AthenaClient, ClientWorker
from athena.app_server.events import FairMux, Subscription
from athena.app_server.execution import ExecutionAdapter
from athena.app_server.server import MessageProcessor
from athena.app_server.transport import (
    DEFAULT_CONTROL_CAPACITY,
    DEFAULT_EVENT_CAPACITY,
    Transport,
)

logger = logging.getLogger(__name__)
DEFAULT_STARTUP_TIMEOUT = 5.0
DEFAULT_SHUTDOWN_TIMEOUT = 5.0


class ThreadEventHandlerRegistry:
    """Thread 事件处理器注册表 — 管理 Thread 生命周期回调的注册与清理。"""

    def __init__(self):
        self._handlers: dict[str, object] = {}

    async def attach(self, thread_id: str) -> None:
        """为 Thread 注册事件处理回调。"""
        self._handlers.setdefault(thread_id, None)

    async def stop_all(self) -> None:
        """清理所有已注册的处理器。"""
        self._handlers.clear()


class SubscriptionRegistry:
    """订阅注册表 — 创建/管理 EventJournal 订阅，通过 FairMux 多路复用事件。"""

    def __init__(self, mux: FairMux, manager=None):
        self._mux = mux
        self._manager = manager
        self._subs: dict[str, Subscription] = {}

    async def create(self, thread_id: str, after_sequence=0) -> Subscription:
        # 先校验 thread 存在 — 失败立即报错，不返回僵尸订阅
        if self._manager is not None:
            await self._manager.get(thread_id)  # type: ignore[union-attr]
        sub = Subscription(
            subscription_id=f"sub:{uuid4().hex[:12]}",
            thread_id=thread_id,
            cursor=after_sequence,
        )
        self._subs[sub.subscription_id] = sub
        self._mux.add(sub)
        if self._manager is not None:
            sub.pump_task = asyncio.create_task(
                self._pump(sub), name=f"pump-{sub.subscription_id}"
            )
        return sub

    async def _pump(self, sub: Subscription) -> None:
        try:
            handle = await self._manager.get(sub.thread_id)  # type: ignore[union-attr]
            async for event in handle.events(after_sequence=sub.cursor):
                if not sub.active:
                    return
                await sub.queue.put(event)
                sub.cursor = event.sequence
                sub.has_data.set()
        except asyncio.CancelledError:
            # 订阅 pump 被取消（registry 清理时）→ 正常退出
            pass
        except Exception:
            logger.exception("subscription pump failed: %s", sub.subscription_id)
        finally:
            sub.active = False  # 标记非活跃 — 防止 FairMux 空转
            self._mux.remove(sub.subscription_id)  # 从 mux 清理，防止残留

    async def remove(self, subscription_id: str) -> None:
        sub = self._subs.pop(subscription_id, None)
        if sub is not None:
            sub.active = False
            if sub.pump_task is not None:
                sub.pump_task.cancel()
                try:
                    await sub.pump_task
                except asyncio.CancelledError:
                    # pump_task 已被 cancel → 等待完毕，预期行为
                    pass
            self._mux.remove(subscription_id)

    async def remove_all(self) -> None:
        for sid in list(self._subs):
            await self.remove(sid)


@dataclass
class AppServer:
    """进程内 AppServer 顶层容器——AsyncExitStack 保证清理顺序。"""

    transport: Transport
    server: MessageProcessor
    client: AthenaClient
    manager: object
    event_handlers: ThreadEventHandlerRegistry
    subscriptions: SubscriptionRegistry
    mux: FairMux
    _exit_stack: AsyncExitStack
    _owns_manager: bool = True

    @classmethod
    async def create(
        cls,
        manager,
        *,
        client_name="athena-cli",
        client_version="0.1.0",
        protocol_version=1,
        control_capacity=DEFAULT_CONTROL_CAPACITY,
        event_capacity=DEFAULT_EVENT_CAPACITY,
        max_inflight=64,
        startup_timeout=DEFAULT_STARTUP_TIMEOUT,
        owns_manager=True,
    ) -> "AppServer":
        stack = AsyncExitStack()
        try:
            transport = Transport(
                control_capacity=control_capacity, event_capacity=event_capacity
            )
            stack.push_async_callback(transport.aclose)
            mux = FairMux(transport.send_event)
            await mux.start()
            stack.push_async_callback(mux.stop)
            event_handlers = ThreadEventHandlerRegistry()

            stack.push_async_callback(event_handlers.stop_all)
            subscriptions = SubscriptionRegistry(mux, manager)
            stack.push_async_callback(subscriptions.remove_all)

            server = MessageProcessor(transport, max_inflight=max_inflight)
            executor = ExecutionAdapter(manager, event_handlers, subscriptions)
            server.set_executor(executor)
            server.set_event_system(event_handlers, mux)
            await server.start()

            stack.push_async_callback(lambda: server.shutdown(timeout=1.0))
            client = AthenaClient(
                transport, ClientWorker(transport), owns_transport=False
            )
            await client._worker.start()
            stack.push_async_callback(client._cleanup)
            await client._initialize(
                client_name, client_version, protocol_version, startup_timeout
            )
            return cls(
                transport=transport,
                server=server,
                client=client,
                manager=manager,
                event_handlers=event_handlers,
                subscriptions=subscriptions,
                mux=mux,
                _exit_stack=stack,
                _owns_manager=owns_manager,
            )
        except BaseException:
            # 初始化任何步骤失败（含 KeyboardInterrupt）→ 清理已创建资源后传播
            await stack.aclose()
            raise

    async def shutdown(self, timeout=DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        await self.client.shutdown(timeout=timeout)
        await self._exit_stack.aclose()
        if self._owns_manager and hasattr(self.manager, "aclose"):
            await self.manager.aclose("server_shutdown")

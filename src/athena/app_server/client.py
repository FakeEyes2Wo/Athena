"""客户端门面 — AthenaClient, ClientWorker, Sequencer。

对齐 Codex ``AppServerClient`` / ``InProcessAppServerClient``。
"""

import asyncio
import logging
from typing import Self

from athena.app_server.exceptions import ClosedError, OverloadedError, RpcException
from athena.app_server.protocol import (
    ErrorCode,
    Method,
    ClientNotification,
    EventNotification,
    RequestEnvelope,
    ResponseEnvelope,
    RpcError,
    ServerEvent,
    ServerRequest,
)
from athena.app_server.transport import (
    DEFAULT_CONTROL_CAPACITY,
    DEFAULT_EVENT_CAPACITY,
    Transport,
    ServerRequestReply,
)

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_SHUTDOWN_TIMEOUT = 5.0


class Sequencer:
    """单调递增的 request_id 生成器。0 保留给 initialize。"""

    def __init__(self) -> None:
        self._next = 1

    def next(self) -> int:
        """返回下一个单调递增的 request_id。"""
        if self._next > 2**63 - 1:
            raise OverflowError("request id space exhausted")
        rid = self._next
        self._next += 1
        return rid


class ClientWorker:
    """并发读取 control 和 event 通道，路由到正确的 Future/buffer。"""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._pending: dict[int, asyncio.Future[ResponseEnvelope]] = {}
        self._control_buf: asyncio.Queue[ServerRequest] = asyncio.Queue(16)
        self._event_buf: asyncio.Queue[EventNotification] = asyncio.Queue(256)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动 Worker 后台读取任务。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="client-worker")

    async def stop(self) -> None:
        """取消 Worker 后台任务并等待退出。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # worker task 被 cancel → 等待完毕，预期行为
                pass
            self._task = None

    def register_pending(
        self, request_id: int, future: asyncio.Future[ResponseEnvelope]
    ) -> None:
        """注册 pending Future，收到响应时由 Worker 完成。"""
        self._pending[request_id] = future

    def unregister_pending(
        self, request_id: int
    ) -> asyncio.Future[ResponseEnvelope] | None:
        """取消注册并返回 pending Future。"""
        return self._pending.pop(request_id, None)

    def fail_all_pending(self, error: RpcError) -> None:
        """将所有未完成的请求以错误回复完成并清空。"""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(
                    ResponseEnvelope(request_id=-1, result=None, error=error)
                )
        self._pending.clear()

    async def _run(self) -> None:
        async def _read_control() -> None:
            while True:
                msg = await self._transport.recv_response_or_control()
                if msg is None:
                    await self._event_buf.put(None)
                    return  # type: ignore[arg-type]
                if isinstance(msg, ResponseEnvelope):
                    fut = self._pending.pop(msg.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif isinstance(msg, ServerRequest):
                    try:
                        self._control_buf.put_nowait(msg)
                    except asyncio.QueueFull:
                        # 控制通道满 → 回退到阻塞 put
                        await self._control_buf.put(msg)

        async def _read_events() -> None:
            while True:
                evt = await self._transport.recv_event()
                if evt is None:
                    return
                try:
                    self._event_buf.put_nowait(evt)
                except asyncio.QueueFull:
                    # 事件缓冲区满 → 回退到阻塞 put
                    logger.warning("event buffer full, blocking")
                    await self._event_buf.put(evt)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_read_control())
            tg.create_task(_read_events())

    async def next_event(self, timeout: float | None = None) -> ServerEvent | None:
        """返回下一条 control 或 event 消息，超时返回 None。"""
        try:
            return self._control_buf.get_nowait()
        except asyncio.QueueEmpty:
            # 控制通道已空 → 回退到异步等待，同时监听 control 和 event
            pass
        # 同时等待 control 和 event — 审批等控制消息到达时立即返回
        ctrl_task = asyncio.ensure_future(self._control_buf.get())
        evt_task = asyncio.ensure_future(self._event_buf.get())
        tasks = [ctrl_task, evt_task]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED, timeout=timeout
            )
        except asyncio.TimeoutError:
            # 等待超时 → 取消所有挂起任务，返回 None
            for t in tasks:
                t.cancel()
            return None
        for t in pending:
            t.cancel()
        # 取第一条，其余放回 — 防止同时到达时丢失
        result = None
        for t in done:
            r = t.result()
            if result is None:
                result = r
            else:
                self._put_back(r)
        return result

    def _put_back(self, item: ServerEvent) -> None:
        """将未消费的事件放回对应队列。"""
        if isinstance(item, ServerRequest):
            try:
                self._control_buf.put_nowait(item)
            except asyncio.QueueFull:
                # 缓冲区满 → 静默丢弃（回退项不是关键消息）
                pass
        elif isinstance(item, EventNotification):
            try:
                self._event_buf.put_nowait(item)
            except asyncio.QueueFull:
                # 缓冲区满 → 静默丢弃（回退项不是关键消息）
                pass


class AthenaClient:
    """进程中 Client facade。每个 asyncio event loop 一个实例。"""

    def __init__(
        self, transport: Transport, worker: ClientWorker, owns_transport=True
    ) -> None:
        self._transport = transport
        self._worker = worker
        self._sequencer = Sequencer()
        self._owns_transport = owns_transport
        self._closing = False

    @classmethod
    async def start(
        cls,
        *,
        client_name="athena-cli",
        client_version="0.1.0",
        protocol_version=1,
        control_capacity=DEFAULT_CONTROL_CAPACITY,
        event_capacity=DEFAULT_EVENT_CAPACITY,
        startup_timeout=5.0,
    ) -> Self:
        """独立客户端 — 需要外部 AppServer 配合，否则必然超时。

        推荐使用 ``AppServer.create(manager)`` 同时创建服务端和客户端。
        """
        transport = Transport(
            control_capacity=control_capacity, event_capacity=event_capacity
        )
        worker = ClientWorker(transport)
        await worker.start()
        client = cls(transport, worker, owns_transport=True)
        try:
            init_resp = await client._raw_request(
                Method.INITIALIZE,
                0,
                {
                    "client_name": client_name,
                    "client_version": client_version,
                    "protocol_version": protocol_version,
                },
                startup_timeout,
            )
            if "error" in init_resp:
                raise RpcException(
                    init_resp["error"].get("code", -1),
                    init_resp["error"].get("message", "initialize failed"),
                )
        except asyncio.TimeoutError:
            # 等待 initialize 响应超时 → 清理资源并提示使用 AppServer.create()
            await client._cleanup()
            raise RuntimeError(
                "server did not respond — standalone start() requires an "
                "externally created AppServer. Use AppServer.create() instead."
            )
        except Exception:
            # 初始化握手未预期异常 → 清理资源后原样传播
            await client._cleanup()
            raise
        await client.notify(Method.INITIALIZED)
        try:
            await asyncio.wait_for(transport.wait_ready(), timeout=startup_timeout)
        except asyncio.TimeoutError:
            # Server ready 信号超时 → 清理资源
            await client._cleanup()
            raise RuntimeError("server did not become ready in time")
        return client

    async def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout=DEFAULT_REQUEST_TIMEOUT,
    ) -> dict:
        """向 Server 发送请求，返回结果的 result 部分。RPC 错误转为 RpcException。"""
        rid = self._sequencer.next()
        response = await self._raw_request(method, rid, params or {}, timeout)
        if "error" in response:
            err = response["error"]
            raise RpcException(
                err.get("code", -1), err.get("message", ""), err.get("data")
            )
        return response.get("result", {})

    async def notify(self, method: str, params: dict | None = None) -> None:
        """向 Server 发送单向通知（无响应）。关闭中则静默丢弃。"""
        if self._closing:
            return
        await self._transport.send_notification(
            ClientNotification(method=method, params=params or {})
        )

    async def next_event(self, timeout: float | None = None) -> ServerEvent | None:
        """从 Server 拉取下一条 ServerRequest 或 EventNotification。"""
        return await self._worker.next_event(timeout=timeout)

    async def respond_to_server_request(
        self, server_call_id: str, result: dict
    ) -> None:
        """回复 Server 发来的 ServerRequest（如审批请求）。"""
        await self._transport.send_server_request_reply(
            ServerRequestReply(server_call_id=server_call_id, result=result)
        )

    async def fail_server_request(
        self, server_call_id: str, error_message: str
    ) -> None:
        """以 INTERNAL 错误码回复 ServerRequest。"""
        await self._transport.send_server_request_reply(
            ServerRequestReply(
                server_call_id=server_call_id,
                error_code=ErrorCode.INTERNAL.value,
                error_message=error_message,
            )
        )

    async def shutdown(self, timeout=DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """向 Server 发送关闭请求并清理本地资源。"""
        if self._closing:
            return
        try:
            await asyncio.wait_for(
                self.request(Method.SERVER_SHUTDOWN, timeout=timeout), timeout=timeout
            )
        except Exception:
            # 关闭请求异常（server 可能已提前关闭）→ 吞掉并继续本地清理
            logger.debug("shutdown request failed (server may already be closing)")
        self._closing = True
        await self._cleanup()

    async def _cleanup(self) -> None:
        self._worker.fail_all_pending(
            RpcError(code=ErrorCode.CLOSED.value, message="client closed")
        )
        await self._worker.stop()
        if self._owns_transport:
            await self._transport.aclose()

    async def _raw_request(
        self, method: str, request_id: int, params: dict, timeout: float
    ) -> dict:
        if self._closing:
            raise ClosedError("client is closing")
        fut: asyncio.Future[ResponseEnvelope] = asyncio.get_event_loop().create_future()
        self._worker.register_pending(request_id, fut)
        try:
            await self._transport.send_request(
                RequestEnvelope(request_id=request_id, method=method, params=params),
                timeout=timeout,
            )
        except OverloadedError:
            # transport 过载拒绝发送 → 注销 pending 后原样传播
            self._worker.unregister_pending(request_id)
            raise
        try:
            response = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # RPC 响应等待超时 → 清理注册后传播超时
            self._worker.unregister_pending(request_id)
            raise
        except asyncio.CancelledError:
            # RPC 请求被取消 → 清理注册后传播取消
            self._worker.unregister_pending(request_id)
            raise
        result: dict = {"result": None}
        if response.result is not None:
            result["result"] = response.result
        if response.error is not None:
            result["error"] = {
                "code": response.error.code,
                "message": response.error.message,
                "data": response.error.data,
            }
        return result


if __name__ == "__main__":
    import asyncio
    from athena.app_server.thread_manager import RuntimeThreadManager
    from athena.app_server.lifecycle import AppServer

    async def _demo_runner(thread, turn, emit):
        """示例 runner——收到请求后写几条消息，然后返回结果。"""
        await emit("message", f"artifact://events/{turn.turn_id}/ack")
        await emit("message", f"artifact://events/{turn.turn_id}/processing")
        return (
            f"result://{turn.turn_id}",
            f"context://{turn.turn_id}/next",
        )

    async def main():
        print("=== Athena Client Demo ===\n")

        # 1. 创建 ThreadManager + AppServer（内部完成 initialize 握手）
        manager = RuntimeThreadManager(_demo_runner)
        app = await AppServer.create(manager, owns_manager=True)
        client = app.client
        print(f"1. Server ready: {app.server.state}")

        # 2. 创建 Thread
        thread_resp = await client.request(
            "thread/start",
            {
                "session_id": "demo-session",
                "context_ref": "artifact://demo/initial-context",
            },
        )
        thread_id = thread_resp["thread_id"]
        print(f"2. Thread created: {thread_id}")

        # 3. 提交 Turn（不等 runner 完成）
        turn_resp = await client.request(
            "turn/start",
            {
                "thread_id": thread_id,
                "request_ref": "artifact://demo/request",
            },
        )
        turn_id = turn_resp["turn_id"]
        print(f"3. Turn submitted: {turn_id}  (runner is running...)")

        # 4. 订阅该 Thread 的事件
        sub_resp = await client.request(
            "thread/subscribe",
            {
                "thread_id": thread_id,
                "after_sequence": 0,
            },
        )
        sub_id = sub_resp["subscription_id"]
        print(f"4. Subscribed: {sub_id}")

        # 5. 消费事件直到 turn_completed
        print("5. Events:")
        while True:
            event = await client.next_event(timeout=5.0)
            if event is None:
                print("   (no event, timeout)")
                break
            # 事件通知
            if hasattr(event, "kind"):
                print(f"   [{event.sequence}] {event.kind}  turn={event.turn_id}")
                if event.kind in ("turn_completed", "turn_failed"):
                    break
            # ServerRequest（审批等）
            elif hasattr(event, "server_call_id"):
                print(
                    f"   [SERVER REQUEST] {event.method}  call={event.server_call_id}"
                )
                # 自动批准
                await client.respond_to_server_request(
                    event.server_call_id, {"approved": True}
                )

        # 6. 关闭
        await app.shutdown(timeout=2.0)
        print(f"\n6. Shutdown complete.  Server state: {app.server.state}")
        print("=== Demo done ===")

    asyncio.run(main())

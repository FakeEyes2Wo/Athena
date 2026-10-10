"""消息处理器 — Server 状态机、Dispatcher、admission 控制。

对齐 Codex ``MessageProcessor``。负责握手/校验/路由/登记 in-flight Task。
执行逻辑委托给 ExecutionAdapter。
"""

import asyncio
import logging
from typing import Literal
from uuid import uuid4

from athena.app_server.exceptions import ClosedError
from athena.app_server.protocol import (
    ErrorCode,
    Method,
    ClientNotification,
    RequestEnvelope,
    ResponseEnvelope,
    ServerRequest,
    map_exception_to_error_code,
    rpc_error,
)
from athena.app_server.transport import ServerRequestReply, Transport

logger = logging.getLogger(__name__)

DEFAULT_MAX_INFLIGHT = 64
DEFAULT_SHUTDOWN_TIMEOUT = 5.0
RESERVED_INIT_ID = 0

ServerState = Literal[
    "CREATED",
    "INITIALIZING",
    "READY",
    "DRAINING",
    "TERMINATED",
    "FAILED",
    "FORCE_CLOSED",
]


def _serialize_result(result: object) -> dict:
    """将执行结果转为可 JSON 序列化的 dict。"""
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if hasattr(result, "__dict__"):
        return {k: v for k, v in result.__dict__.items() if not k.startswith("_")}
    return {"value": str(result)}


class MessageProcessor:
    """Server 侧请求入口——状态机 + Dispatcher + admission gate。"""

    def __init__(
        self,
        transport: Transport,
        *,
        max_inflight=DEFAULT_MAX_INFLIGHT,
        max_pending: int | None = None,
    ) -> None:
        if max_inflight < 1 or (max_pending is not None and max_pending < 1):
            raise ValueError("request limits must be positive")
        self._transport = transport
        self._max_pending = max_inflight if max_pending is None else max_pending
        self._state: ServerState = "CREATED"
        self._inflight: dict[int, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(max_inflight)
        self._ready = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._pending_server_calls: dict[str, asyncio.Future[dict]] = {}
        self._executor = None

    def set_executor(self, executor) -> None:
        """注入 ExecutionAdapter — 将协议方法映射到 ThreadManager 调用。"""
        self._executor = executor

    # 生命周期

    async def start(self) -> None:
        """启动 dispatcher 接收循环，从 CREATED 进入 INITIALIZING 状态。"""
        if self._state != "CREATED":
            return
        self._state = "INITIALIZING"
        self._dispatcher_task = asyncio.create_task(
            self._dispatch(), name="msg-processor-dispatch"
        )

    async def shutdown(self, timeout=DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """优雅关闭：等待 in-flight 任务完成或超时后取消，停止 dispatcher。"""
        if self._state in ("TERMINATED", "FAILED", "FORCE_CLOSED"):
            return
        self._state = "DRAINING"
        self._fail_server_calls()
        if self._inflight:
            tasks = list(self._inflight.values())
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
                )
            except asyncio.TimeoutError:
                # in-flight 任务在超时内未完成，强制取消
                logger.warning(
                    "shutdown: %d in-flight tasks did not complete", len(self._inflight)
                )
                for task in tasks:
                    if not task.done():
                        task.cancel()
        self._state = "TERMINATED"
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                # dispatcher 任务已被 cancel，等待其完全退出
                pass
        await self._transport.aclose()

    # 分发器

    async def _dispatch(self) -> None:
        """接收循环 — 控制消息直接处理，普通请求入队不阻塞。

        将 semaphore 等待和 inflight 检查从接收循环移入 worker，
        避免满载时阻断控制消息，同时消除重复 request_id 的 TOCTOU 竞态。
        """
        pending: asyncio.Queue[RequestEnvelope] = asyncio.Queue(self._max_pending)
        queued_ids: set[int] = set()

        async def _worker() -> None:
            while True:
                await self._slots.acquire()
                try:
                    msg = await pending.get()
                    rid = msg.request_id
                    queued_ids.remove(rid)
                    if self._state in (
                        "DRAINING",
                        "TERMINATED",
                        "FAILED",
                        "FORCE_CLOSED",
                    ):
                        await self._send_error(
                            rid, ErrorCode.CLOSED, "server shutting down"
                        )
                        self._slots.release()
                        continue
                    task = asyncio.create_task(
                        self._execute_and_reply(msg), name=f"srv-request-{rid}"
                    )
                    self._inflight[rid] = task
                    task.add_done_callback(lambda t, r=rid: self._cleanup(r, t))
                except BaseException:
                    # Worker startup/cancellation failed before a task owned the slot.
                    self._slots.release()
                    raise

        worker_task = asyncio.create_task(_worker(), name="srv-worker")
        try:
            while True:
                msg = await self._transport.recv_client_message()
                if msg is None:
                    await self._do_shutdown("transport_closed")
                    return
                if isinstance(msg, ClientNotification):
                    await self._handle_notification(msg)
                    continue
                if isinstance(msg, ServerRequestReply):
                    await self._handle_server_request_reply(msg)
                    continue
                if not isinstance(msg, RequestEnvelope):
                    continue
                rid = msg.request_id
                if msg.method == Method.SERVER_SHUTDOWN:
                    result = await self._handle_shutdown_request(msg)
                    await self._transport.send_response(
                        ResponseEnvelope(request_id=rid, result=result)
                    )
                    return  # 直接退出 dispatcher，让外部 shutdown() 做清理
                if not self._request_allowed(msg):
                    await self._reject_for_state(msg)
                    continue
                if rid in queued_ids or rid in self._inflight:
                    await self._send_error(
                        rid, ErrorCode.DUPLICATE_REQUEST_ID, "duplicate request id"
                    )
                    continue
                try:
                    pending.put_nowait(msg)
                except asyncio.QueueFull:
                    # Execution backlog is full; keep control messages reachable.
                    await self._send_error(
                        rid, ErrorCode.OVERLOADED, "server request queue full"
                    )
                else:
                    queued_ids.add(rid)
        except asyncio.CancelledError:
            # dispatcher 循环被 cancel — 正常退出
            pass
        finally:
            self._fail_server_calls()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            # Bug 8 fix: 排空 pending 队列，回复 ClosedError
            while not pending.empty():
                msg = pending.get_nowait()
                await self._reply_closed(msg.request_id)

    # 请求执行

    async def _execute_and_reply(self, msg: RequestEnvelope) -> None:
        rid = msg.request_id
        try:
            if msg.method == Method.INITIALIZE:
                result = await self._handle_initialize(msg)
            elif self._executor is not None:
                result = await self._executor.execute(msg.method, msg.params or {})
            else:
                raise RuntimeError("no executor configured")
            await self._transport.send_response(
                ResponseEnvelope(
                    request_id=rid,
                    result=_serialize_result(result),
                )
            )
        except asyncio.CancelledError:
            # Shutdown cancelled this request; never wait for a stopped reader.
            await self._reply_closed(rid)
            raise
        except Exception as exc:
            # 执行过程中发生异常 — 映射为协议错误码并回复
            await self._transport.send_response(
                ResponseEnvelope(
                    request_id=rid,
                    error=rpc_error(map_exception_to_error_code(exc), "request failed"),
                )
            )

    def _cleanup(self, request_id: int, task: asyncio.Task[None]) -> None:
        self._inflight.pop(request_id, None)
        self._slots.release()
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("unhandled exception in request %d: %s", request_id, exc)

    # 特殊方法

    async def _handle_initialize(self, msg: RequestEnvelope) -> dict:
        if msg.request_id != RESERVED_INIT_ID:
            raise ValueError("initialize must use request_id=0")
        if self._state != "INITIALIZING":
            raise RuntimeError("already initialized")
        params = msg.params or {}
        if params.get("protocol_version", 1) != 1:
            raise ValueError("unsupported protocol version")
        return {"protocol_version": 1, "server_name": "athena", "status": "ok"}

    async def _handle_shutdown_request(self, msg: RequestEnvelope) -> dict:
        # 不在此处同步调用 shutdown()——它在 _dispatch() 内部执行，
        # 会尝试 cancel 并 await 自己，造成死锁。只设状态，让外部清理。
        if self._state not in ("DRAINING", "TERMINATED", "FAILED", "FORCE_CLOSED"):
            self._state = "DRAINING"
        return {"status": "shutting_down"}

    async def _handle_notification(self, msg: ClientNotification) -> None:
        if msg.method == Method.INITIALIZED:
            if self._state != "INITIALIZING":
                logger.warning("initialized received in state %s", self._state)
                return
            self._state = "READY"
            self._ready.set()
            self._transport.set_ready()
            logger.info("server ready")

    async def _handle_server_request_reply(self, reply) -> None:
        fut = self._pending_server_calls.pop(reply.server_call_id, None)
        if fut is not None and not fut.done():
            if reply.error_code is not None:
                fut.set_exception(RuntimeError(reply.error_message))
            else:
                fut.set_result(reply.result or {})

    async def _do_shutdown(self, reason: str) -> None:
        """标记关闭 — 不在此处 cancel dispatcher task（可能在 dispatcher 内调用）。"""
        if self._state in ("DRAINING", "TERMINATED", "FAILED", "FORCE_CLOSED"):
            return
        self._state = "DRAINING"
        self._fail_server_calls()

    def _fail_server_calls(self) -> None:
        """Wake pending approval/input callers when the connection stops."""
        pending, self._pending_server_calls = self._pending_server_calls, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(ClosedError("server shutting down"))

    # Server→Client 调用

    async def _call_client(self, method: str, params: dict, timeout: float) -> dict:
        if self._state in ("DRAINING", "TERMINATED", "FAILED", "FORCE_CLOSED"):
            raise ClosedError("server shutting down")
        call_id = f"s:{uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._pending_server_calls[call_id] = future
        send = asyncio.create_task(
            self._transport.send_server_request(
                ServerRequest(server_call_id=call_id, method=method, params=params)
            )
        )
        try:
            async with asyncio.timeout(timeout):
                # Shutdown must also wake a caller blocked on a full send queue.
                await asyncio.wait((send, future), return_when=asyncio.FIRST_COMPLETED)
                if future.done():
                    return await future
                await send
                return await future
        finally:
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)
            self._pending_server_calls.pop(call_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def request_approval(
        self, thread_id: str, turn_id: str, message: str, timeout=300.0
    ) -> bool:
        """向 Client 发送审批请求，等待用户批准或拒绝。超时默认返回 False。"""
        try:
            result = await self._call_client(
                Method.ITEM_APPROVAL_REQUEST,
                {"thread_id": thread_id, "turn_id": turn_id, "message": message},
                timeout,
            )
            return result.get("approved", False)
        except asyncio.TimeoutError:
            # Client 在超时内未响应审批请求
            return False

    async def request_user_input(
        self, thread_id: str, turn_id: str, questions: list[dict], timeout=300.0
    ) -> dict | None:
        """向 Client 发送提问请求，等待用户回答。超时返回 None，取消向上传播。

        ``questions`` 是 ``UserInputQuestion`` 的 dict 形式：
        ``{"id": ..., "question": ..., ...}``。回复形如
        ``{"answers": {qid: {"answers": [...]}}}``。
        """
        try:
            return await self._call_client(
                Method.ITEM_USER_INPUT_REQUEST,
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "questions": questions,
                },
                timeout,
            )
        except asyncio.TimeoutError:
            # Client 在超时内未响应提问请求
            return None

    # 辅助

    def _request_allowed(self, msg: RequestEnvelope) -> bool:
        if Method.is_control_method(msg.method):
            return True
        return self._state == "READY"

    async def _reject_for_state(self, msg: RequestEnvelope) -> None:
        match self._state:
            case "INITIALIZING":
                code, text = ErrorCode.NOT_INITIALIZED, "server not initialized"
            case "DRAINING":
                code, text = ErrorCode.CLOSED, "server is draining"
            case _:
                code, text = ErrorCode.CLOSED, f"server in state {self._state}"
        await self._send_error(msg.request_id, code, text)

    async def _send_error(self, request_id: int, code: ErrorCode, message: str) -> None:
        await self._transport.send_response(
            ResponseEnvelope(request_id=request_id, error=rpc_error(code, message))
        )

    async def _reply_closed(self, request_id: int) -> None:
        try:
            async with asyncio.timeout(0):
                await self._send_error(
                    request_id, ErrorCode.CLOSED, "server shutting down"
                )
        except (TimeoutError, ClosedError):
            # Full or closed response lane: EOF will settle pending client calls.
            pass

    @property
    def state(self) -> str:
        """Expose the request admission lifecycle state."""
        return self._state

    @property
    def ready(self) -> asyncio.Event:
        """Signal that the client handshake has completed."""
        return self._ready

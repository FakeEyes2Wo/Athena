"""传输排队层 — 三条逻辑通道，有界队列，背压，幂等关闭。

对齐 Codex ``in_process.rs``：command 通道用 try_send + WouldBlock 语义，
response/control 必达，event 独立通道不被 control 阻塞。
"""

import asyncio

from athena.app_server.exceptions import ClosedError, OverloadedError
from athena.app_server.protocol import (
    ClientNotification,
    EventNotification,
    RequestEnvelope,
    ResponseEnvelope,
    ServerRequest,
)

DEFAULT_CONTROL_CAPACITY = 64
DEFAULT_EVENT_CAPACITY = 256


class Transport:
    """进程内双通道传输——三条逻辑通道复用两个物理 ``asyncio.Queue``。"""

    def __init__(
        self,
        control_capacity=DEFAULT_CONTROL_CAPACITY,
        event_capacity=DEFAULT_EVENT_CAPACITY,
    ) -> None:
        cap_c = max(control_capacity, 1)
        cap_e = max(event_capacity, 1)
        self._c2s: asyncio.Queue = asyncio.Queue(cap_c)
        self._s2c_control: asyncio.Queue = asyncio.Queue(cap_c)
        self._s2c_event: asyncio.Queue = asyncio.Queue(cap_e)
        self._closed = False
        self._ready = asyncio.Event()
        self._changed = asyncio.Event()

    # Client 侧

    async def _put(self, queue: asyncio.Queue, item, *, timeout=None) -> None:
        async with asyncio.timeout(timeout):
            while True:
                if self._closed:
                    raise ClosedError("transport is closed")
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    # Lane full; wait for a dequeue or close to wake this writer.
                    self._changed.clear()
                    await self._changed.wait()
                else:
                    self._changed.set()
                    return

    async def _recv(self, queue: asyncio.Queue):
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                # Lane empty; wait for a write or close before checking again.
                if self._closed:
                    return None
                self._changed.clear()
                await self._changed.wait()
            else:
                self._changed.set()
                return item

    async def send_request(self, envelope: RequestEnvelope, *, timeout=5.0) -> None:
        """Enqueue a request, raising OverloadedError when its lane stays full."""
        try:
            await self._put(self._c2s, envelope, timeout=timeout)
        except asyncio.TimeoutError:
            # Request enqueue timed out because the control lane stayed full.
            raise OverloadedError("control channel full") from None

    async def send_notification(self, notification: ClientNotification) -> None:
        """Best-effort enqueue a notification; drop it when the lane is full."""
        if self._closed:
            raise ClosedError("transport is closed")
        try:
            self._c2s.put_nowait(notification)
        except asyncio.QueueFull:
            # Notifications are best-effort when the client command lane is full.
            pass
        else:
            self._changed.set()

    async def send_server_request_reply(self, reply: "ServerRequestReply") -> None:
        """Enqueue a client reply to a server-originated request."""
        await self._put(self._c2s, reply)

    async def recv_response_or_control(
        self,
    ) -> "ResponseEnvelope | ServerRequest | None":
        """Receive a response or server request, or None after close."""
        return await self._recv(self._s2c_control)

    async def recv_event(self) -> EventNotification | None:
        """Receive one event notification, or None after close."""
        return await self._recv(self._s2c_event)

    async def wait_ready(self) -> None:
        """Wait until ready, raising ClosedError if closed first."""
        while not self._ready.is_set():
            if self._closed:
                raise ClosedError("transport is closed")
            self._changed.clear()
            await self._changed.wait()

    def set_ready(self) -> None:
        """Mark the transport ready and wake readiness waiters."""
        self._ready.set()
        self._changed.set()

    async def recv_client_message(
        self,
    ) -> "RequestEnvelope | ClientNotification | ServerRequestReply | None":
        """Receive one client message, or None after close."""
        return await self._recv(self._c2s)

    async def send_response(self, envelope: ResponseEnvelope) -> None:
        """Enqueue a server response."""
        await self._put(self._s2c_control, envelope)

    async def send_server_request(self, request: ServerRequest) -> None:
        """Enqueue a server-originated client request."""
        await self._put(self._s2c_control, request)

    async def send_event(self, notification: EventNotification) -> None:
        """Enqueue an event notification on its independent lane."""
        await self._put(self._s2c_event, notification)

    async def aclose(self) -> None:
        """Close the transport and wake blocked readers and writers."""
        if self._closed:
            return
        self._closed = True
        self._changed.set()

    @property
    def closed(self) -> bool:
        """传输是否已关闭。"""
        return self._closed


class ServerRequestReply:
    """Client 对 ServerRequest 的回复。"""

    def __init__(
        self,
        server_call_id: str,
        result: dict | None = None,
        error_code: int | None = None,
        error_message: str = "",
    ) -> None:
        self.server_call_id = server_call_id
        self.result = result
        self.error_code = error_code
        self.error_message = error_message

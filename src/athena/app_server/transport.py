"""传输排队层 — 三条逻辑通道，有界队列，背压，幂等关闭。

对齐 Codex ``in_process.rs``：command 通道用 try_send + WouldBlock 语义，
response/control 必达，event 独立通道不被 control 阻塞。
"""

import asyncio

from athena.app_server.exceptions import OverloadedError
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
        self._eof_queues: set[int] = set()  # 追踪已发 EOF 的队列，取消后重试不重复

    # Client 侧

    async def send_request(self, envelope: RequestEnvelope, *, timeout=5.0) -> None:
        """投递请求信封到控制通道；通道满时抛 OverloadedError。"""
        try:
            await asyncio.wait_for(self._c2s.put(envelope), timeout=timeout)
        except asyncio.TimeoutError:
            # 控制通道满，规定时间内无法放入 → 拒绝请求
            raise OverloadedError("control channel full") from None

    async def send_notification(self, notification: ClientNotification) -> None:
        """投递通知（尽力而为，通道满则丢弃）。"""
        try:
            self._c2s.put_nowait(notification)
        except asyncio.QueueFull:
            # 通知通道满 → 丢弃通知（尽力而为语义）
            pass

    async def send_server_request_reply(self, reply: "ServerRequestReply") -> None:
        """投递 Client 对 ServerRequest 的回复到控制通道。"""
        await self._c2s.put(reply)

    async def recv_response_or_control(
        self,
    ) -> "ResponseEnvelope | ServerRequest | None":
        """从服务端控制通道接收响应或服务端请求。"""
        return await self._s2c_control.get()

    async def recv_event(self) -> EventNotification | None:
        """从事件通道接收一个事件通知。"""
        return await self._s2c_event.get()

    async def wait_ready(self) -> None:
        """等待传输就绪信号。"""
        await self._ready.wait()

    def set_ready(self) -> None:
        """置就绪信号，唤醒等待方。"""
        self._ready.set()

    # Server 侧

    async def recv_client_message(
        self,
    ) -> "RequestEnvelope | ClientNotification | ServerRequestReply | None":
        """从客户端控制通道接收一条消息。"""
        return await self._c2s.get()

    async def send_response(self, envelope: ResponseEnvelope) -> None:
        """投递响应信封到服务端控制通道。"""
        await self._s2c_control.put(envelope)

    async def send_server_request(self, request: ServerRequest) -> None:
        """投递服务端请求到控制通道。"""
        await self._s2c_control.put(request)

    async def send_event(self, notification: EventNotification) -> None:
        """投递事件通知到事件通道。"""
        await self._s2c_event.put(notification)

    # 双端

    async def aclose(self) -> None:
        if self._closed:
            return
        for i, q in enumerate((self._c2s, self._s2c_control, self._s2c_event)):
            if i in self._eof_queues:
                continue
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # 队列满无法放入 EOF 哨兵 → 回退到阻塞 put
                try:
                    await asyncio.wait_for(q.put(None), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    # 超时或取消 → 队列仍阻塞，跳过（closed 标记后不再接受新消息）
                    pass
            self._eof_queues.add(i)
        self._closed = True

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

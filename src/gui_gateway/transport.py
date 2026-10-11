"""WebSocket transport — bridges WS messages to the app_server protocol layer."""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from websockets.asyncio.server import Server, ServerConnection, serve

from athena.app_server.protocol import (
    RequestEnvelope,
    ResponseEnvelope,
    map_exception_to_error_code,
    rpc_error,
)
from athena.research.supervisor.events import redact

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gui_gateway.handler import GuiRequestHandler

# 会替换 handler.runtime 的方法：彼此串行执行，且执行后需要改订新 runtime。
RUNTIME_SWAP_METHODS: frozenset[str] = frozenset(
    {"set_project_root", "session_switch", "session_delete"}
)


def _error_message(exc: Exception) -> str:
    """异常的可展示原因：脱敏后的 ``str(exc)``，为空时退回异常类型名。

    前端只能看到这段文本，统一文案会把「会话被占用/路径非法/runtime 已关闭」
    压成同一句话；``redact`` 保证异常里携带的密钥不会随 payload 出网。
    """
    detail = redact(str(exc)).strip()
    return detail or type(exc).__name__


class WebSocketTransport:
    """Bridges WebSocket messages to the existing app_server protocol layer.

    Supports both request/response RPC and server-pushed events.
    Events are sent as JSON messages with a ``kind`` field.
    """

    def __init__(self, handler: "GuiRequestHandler") -> None:
        self._handler = handler
        # 替换 runtime 的方法彼此串行：同一时刻只允许一次项目/会话切换。
        self._swap_lock = asyncio.Lock()

    async def handle(self, ws: ServerConnection) -> None:
        """Handle a single WebSocket connection.

        Each request is dispatched in its own task so a long-running call never
        starves the connection: ``task_clarification_start`` waits on the human
        broker until the user answers, and that answer arrives as a separate
        ``human_reply`` request on the same socket. Runtime-swapping methods
        stay serialized. Events emitted by the runtime are forwarded as
        separate JSON messages.
        """

        # ``websockets`` 不允许并发 send：响应帧与订阅者推送的事件帧可能来自不同
        # 协程（supervisor 在后台任务里流式 emit），用一把锁串行化所有写出。
        send_lock = asyncio.Lock()

        async def send(message: str) -> None:
            async with send_lock:
                await ws.send(message)

        async def emit_event(kind: str, data: dict[str, Any]) -> None:
            """Forward handler-emitted events to the WebSocket client."""
            event_msg = json.dumps({"kind": kind, "data": data}, ensure_ascii=False)
            await send(event_msg)

        subscription = {"runtime": self._handler.runtime, "id": 0}
        subscription["id"] = subscription["runtime"].subscribe(emit_event)

        def follow_runtime() -> None:
            """``set_project_root``/``session_switch`` 会替换 handler.runtime；
            切换后需重新订阅新 runtime 的事件流，否则后续 state/output 事件会丢。"""
            new_runtime = self._handler.runtime
            if new_runtime is subscription["runtime"]:
                return
            subscription["runtime"].unsubscribe(subscription["id"])
            subscription["id"] = new_runtime.subscribe(emit_event)
            subscription["runtime"] = new_runtime

        async def serve_request(raw: str | bytes) -> None:
            request_id = 0
            method = "?"
            try:
                data: dict[str, Any] = json.loads(raw)
                req = RequestEnvelope.model_validate(data)
                request_id = req.request_id
                method = req.method
                if method in RUNTIME_SWAP_METHODS:
                    async with self._swap_lock:
                        result = await self._handler.dispatch(method, req.params or {})
                        follow_runtime()
                else:
                    result = await self._handler.dispatch(method, req.params or {})
                    follow_runtime()
                resp = ResponseEnvelope(
                    request_id=req.request_id,
                    result=result,
                    error=None,
                )
                await send(resp.model_dump_json())
            except Exception as exc:
                logger.exception("dispatch %s failed", method)
                error_data: dict[str, Any] = {
                    "exception": type(exc).__name__,
                    "method": method,
                }
                if hasattr(exc, "code"):
                    error_data["code"] = str(getattr(exc, "code"))
                if hasattr(exc, "retryable"):
                    error_data["retryable"] = bool(getattr(exc, "retryable"))
                if hasattr(exc, "current_revision"):
                    error_data["current_revision"] = getattr(exc, "current_revision")
                err = ResponseEnvelope(
                    request_id=request_id,
                    result=None,
                    error=rpc_error(
                        map_exception_to_error_code(exc),
                        _error_message(exc),
                        error_data,
                    ),
                )
                try:
                    await send(err.model_dump_json())
                except Exception:
                    # 连接已断开：响应无处可发，记录后放弃即可。
                    logger.debug("dropping response for %s", method, exc_info=True)

        in_flight: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                task = asyncio.create_task(serve_request(raw))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            # 与串行实现一致：连接关闭时让在途请求自然结束，再退订事件流。
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            subscription["runtime"].unsubscribe(subscription["id"])

    async def serve(self, host: str = "127.0.0.1", port: int = 0) -> Server:
        """启动 WebSocket 服务器并返回 Server 句柄。

        ``ping_interval=None`` 关闭 keepalive ping：Rust 侧的 tokio-tungstenite 在
        split 后无法及时 flush pong，慢速 PREPARE（真实 LLM 调用）会超过默认
        20s ping_timeout 而被服务端断开，导致流式输出中断。本地回环连接无需心跳。
        """
        return await serve(self.handle, host, port, ping_interval=None)

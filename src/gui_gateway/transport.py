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

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gui_gateway.handler import GuiRequestHandler


class WebSocketTransport:
    """Bridges WebSocket messages to the existing app_server protocol layer.

    Supports both request/response RPC and server-pushed events.
    Events are sent as JSON messages with a ``kind`` field.
    """

    def __init__(self, handler: "GuiRequestHandler") -> None:
        self._handler = handler

    async def handle(self, ws: ServerConnection) -> None:
        """Handle a single WebSocket connection.

        Reads messages sequentially, dispatches each, and sends the response.
        Events emitted by the handler are forwarded as separate JSON messages.
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

        current_runtime = self._handler.runtime
        subscription_id = current_runtime.subscribe(emit_event)
        try:
            async for raw in ws:
                request_id = 0
                try:
                    data: dict[str, Any] = json.loads(raw)
                    req = RequestEnvelope.model_validate(data)
                    request_id = req.request_id
                    result = await self._handler.dispatch(req.method, req.params or {})
                    # ``set_project_root`` 会替换 handler.runtime；切换后需重新订阅
                    # 新 runtime 的事件流，否则后续 state/output 事件会丢。
                    new_runtime = self._handler.runtime
                    if new_runtime is not current_runtime:
                        current_runtime.unsubscribe(subscription_id)
                        subscription_id = new_runtime.subscribe(emit_event)
                        current_runtime = new_runtime
                    resp = ResponseEnvelope(
                        request_id=req.request_id,
                        result=result,
                        error=None,
                    )
                    await send(resp.model_dump_json())
                except Exception as exc:
                    logger.exception("dispatch %s failed", req.method if "req" in locals() else "?")
                    err = ResponseEnvelope(
                        request_id=request_id,
                        result=None,
                        error=rpc_error(
                            map_exception_to_error_code(exc),
                            "request failed",
                        ),
                    )
                    await send(err.model_dump_json())
        finally:
            current_runtime.unsubscribe(subscription_id)

    async def serve(self, host: str = "127.0.0.1", port: int = 0) -> Server:
        """启动 WebSocket 服务器并返回 Server 句柄。

        ``ping_interval=None`` 关闭 keepalive ping：Rust 侧的 tokio-tungstenite 在
        split 后无法及时 flush pong，慢速 PREPARE（真实 LLM 调用）会超过默认
        20s ping_timeout 而被服务端断开，导致流式输出中断。本地回环连接无需心跳。
        """
        return await serve(self.handle, host, port, ping_interval=None)

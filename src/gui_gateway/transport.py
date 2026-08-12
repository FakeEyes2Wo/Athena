"""WebSocket transport — bridges WS messages to the app_server protocol layer."""

import json
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from websockets.asyncio.server import Server, ServerConnection, serve

from athena.app_server.protocol import (
    RequestEnvelope,
    ResponseEnvelope,
    map_exception_to_error_code,
    rpc_error,
)

if TYPE_CHECKING:
    from gui_gateway.handler import GuiRequestHandler

EmitFn = Callable[[str, dict], Awaitable[None]]


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

        async def emit_event(kind: str, data: dict[str, Any]) -> None:
            """Forward handler-emitted events to the WebSocket client."""
            event_msg = json.dumps({"kind": kind, "data": data}, ensure_ascii=False)
            await ws.send(event_msg)

        subscription_id = self._handler._runtime.subscribe(emit_event)
        try:
            async for raw in ws:
                request_id = 0
                try:
                    data: dict[str, Any] = json.loads(raw)
                    req = RequestEnvelope.model_validate(data)
                    request_id = req.request_id
                    result = await self._handler.dispatch(req.method, req.params or {})
                    resp = ResponseEnvelope(
                        request_id=req.request_id,
                        result=result,
                        error=None,
                    )
                    await ws.send(resp.model_dump_json())
                except Exception as exc:
                    err = ResponseEnvelope(
                        request_id=request_id,
                        result=None,
                        error=rpc_error(
                            map_exception_to_error_code(exc),
                            "request failed",
                        ),
                    )
                    await ws.send(err.model_dump_json())
        finally:
            self._handler._runtime.unsubscribe(subscription_id)

    async def serve(self, host: str = "127.0.0.1", port: int = 0) -> Server:
        """启动 WebSocket 服务器并返回 Server 句柄。"""
        return await serve(self.handle, host, port)

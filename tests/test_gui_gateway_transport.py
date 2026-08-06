"""GUI gateway WebSocket 传输层的端到端测试。"""

import json
from typing import Any

import pytest
import websockets

pytestmark = pytest.mark.asyncio


async def test_server_starts_and_prints_port() -> None:
    """服务端启动并返回有效端口，接受 WS 连接并响应 ping。"""
    from gui_gateway.__main__ import start_server

    server, port = await start_server(test_mode=True)
    assert port > 0

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"request_id": 1, "method": "ping", "params": {}}))
        resp: dict[str, Any] = json.loads(await ws.recv())
        assert resp["request_id"] == 1
        assert "result" in resp
        assert resp["result"] == {"pong": True}

    server.close()
    await server.wait_closed()


async def test_unknown_method_returns_error() -> None:
    """未知方法返回错误响应。"""
    from gui_gateway.__main__ import start_server

    server, port = await start_server(test_mode=True)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(
            json.dumps({"request_id": 2, "method": "unknown_method", "params": {}})
        )
        resp: dict[str, Any] = json.loads(await ws.recv())
        assert resp["request_id"] == 2
        assert resp["error"]["code"] == -32602
        assert resp["error"]["message"] == "request failed"

    server.close()
    await server.wait_closed()


async def test_invalid_json_returns_error() -> None:
    """格式错误的 JSON 返回错误响应。"""
    from gui_gateway.__main__ import start_server

    server, port = await start_server(test_mode=True)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send("not valid json")
        resp: dict[str, Any] = json.loads(await ws.recv())
        assert "error" in resp

    server.close()
    await server.wait_closed()

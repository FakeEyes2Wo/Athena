"""GUI gateway WebSocket 传输层的端到端测试。"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import websockets

pytestmark = pytest.mark.asyncio


async def _recv_response(ws) -> dict[str, Any]:
    """读取直到收到 RPC 响应，跳过服务端推送的 ``kind`` 事件（state 广播）。"""
    while True:
        resp = json.loads(await ws.recv())
        if "kind" not in resp:
            return resp


async def test_server_starts_and_prints_port() -> None:
    """服务端启动并返回有效端口，接受 WS 连接并响应 ping。"""
    from gui_gateway.__main__ import start_server

    server, port = await start_server(test_mode=True)
    assert port > 0

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"request_id": 1, "method": "ping", "params": {}}))
        resp = await _recv_response(ws)
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
        resp = await _recv_response(ws)
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
        resp = await _recv_response(ws)
        assert "error" in resp

    server.close()
    await server.wait_closed()


class _FakeRuntime:
    """Minimal runtime double with subscribe/unsubscribe + snapshot emission."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.tree_path = Path(root) / ".athena" / "research_tree.json"
        self._subscribers: dict[int, Any] = {}
        self._next = 0

    def subscribe(self, emit: Any) -> int:
        self._next += 1
        self._subscribers[self._next] = emit
        # 与真实 runtime 一致：订阅后立即推送一条 state 快照。
        asyncio.get_running_loop().create_task(
            emit("state", {"phase": "idle", "status": "idle", "project_root": self.root})
        )
        return self._next

    def unsubscribe(self, subscription_id: int) -> None:
        self._subscribers.pop(subscription_id, None)

    async def aclose(self) -> None:
        self._subscribers.clear()

    def settings(self) -> dict[str, Any]:
        return {"project_root": self.root}


class _FakeWS:
    """Single-shot WebSocket double: yields one request, records every send."""

    def __init__(self, incoming: list[str]) -> None:
        self.incoming = incoming
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self) -> "_FakeWS":
        self._iter = iter(self.incoming)
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


async def test_transport_resubscribes_after_project_switch() -> None:
    """切换项目后，transport 需重新订阅新 runtime 的事件流。"""
    from gui_gateway.handler import GuiRequestHandler
    from gui_gateway.transport import WebSocketTransport

    target = str(Path.cwd())
    created: list[_FakeRuntime] = []
    old = _FakeRuntime("/old-root")

    def factory(path: str) -> _FakeRuntime:
        runtime = _FakeRuntime(path)
        created.append(runtime)
        return runtime

    handler = GuiRequestHandler(old, factory)
    transport = WebSocketTransport(handler)
    ws = _FakeWS(
        [
            json.dumps(
                {"request_id": 1, "method": "set_project_root", "params": {"path": target}}
            )
        ]
    )

    await transport.handle(ws)
    await asyncio.sleep(0)  # flush pending snapshot-emission tasks

    # 连接时（旧 runtime）与切换后（新 runtime）各推一条 state 快照。
    states = [json.loads(m) for m in ws.sent if json.loads(m).get("kind") == "state"]
    assert len(states) >= 2
    assert states[-1]["data"]["project_root"] == target

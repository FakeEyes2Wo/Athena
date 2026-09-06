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

    server, port = await start_server(test_mode=True, port=0)
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

    server, port = await start_server(test_mode=True, port=0)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(
            json.dumps({"request_id": 2, "method": "unknown_method", "params": {}})
        )
        resp = await _recv_response(ws)
        assert resp["request_id"] == 2
        assert resp["error"]["code"] == -32602
        assert resp["error"]["message"] == "unsupported GUI method: unknown_method"

    server.close()
    await server.wait_closed()


async def test_invalid_json_returns_error() -> None:
    """格式错误的 JSON 返回错误响应。"""
    from gui_gateway.__main__ import start_server

    server, port = await start_server(test_mode=True, port=0)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send("not valid json")
        resp = await _recv_response(ws)
        assert "error" in resp

    server.close()
    await server.wait_closed()


class _FakeRuntime:
    """Minimal runtime double with subscribe/unsubscribe + snapshot emission."""

    def __init__(
        self,
        root: str,
        state_root: Path | None = None,
        *,
        output_payload: dict[str, Any] | None = None,
        skip_validate: bool = False,
    ) -> None:
        self.root = root
        self.state_root = state_root
        self.output_payload = output_payload
        self.skip_validate = skip_validate
        self.applied_patches: list[dict[str, Any]] = []
        self.tree_path = Path(root) / ".athena" / "research_tree.json"
        self._subscribers: dict[int, Any] = {}
        self._next = 0

    def subscribe(self, emit: Any) -> int:
        self._next += 1
        self._subscribers[self._next] = emit
        # 与真实 runtime 一致：订阅后立即推送一条 state 快照。
        asyncio.get_running_loop().create_task(
            emit(
                "state", {"phase": "idle", "status": "idle", "project_root": self.root}
            )
        )
        if self.output_payload is not None:
            asyncio.get_running_loop().create_task(emit("output", self.output_payload))
        return self._next

    def unsubscribe(self, subscription_id: int) -> None:
        self._subscribers.pop(subscription_id, None)

    async def suspend(self) -> str:
        return "IDLE"

    async def aclose(self) -> None:
        self._subscribers.clear()

    def settings(self) -> dict[str, Any]:
        return {"project_root": self.root, "skip_validate": self.skip_validate}

    async def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Keep a real in-memory settings map behind the gateway boundary."""
        self.applied_patches.append(dict(patch))
        if "skip_validate" in patch:
            self.skip_validate = patch["skip_validate"]
        return self.settings()


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


async def test_transport_preserves_generic_scoped_output_payload() -> None:
    """Generic scope metadata crosses the WebSocket boundary unchanged."""
    from gui_gateway.handler import GuiRequestHandler
    from gui_gateway.transport import WebSocketTransport

    payload = {
        "type": "output",
        "seq": 1,
        "message_id": "msg-1",
        "source": "agent",
        "channel": "text",
        "text": "public update",
        "session_id": "session-1",
        "scope": "task_understanding",
        "scope_id": "draft-1",
    }
    runtime = _FakeRuntime("/root", output_payload=payload)
    transport = WebSocketTransport(GuiRequestHandler(runtime))
    ws = _FakeWS([json.dumps({"request_id": 1, "method": "ping", "params": {}})])

    await transport.handle(ws)
    await asyncio.sleep(0)

    output_frames = [
        decoded
        for message in ws.sent
        if (decoded := json.loads(message)).get("kind") == "output"
    ]
    assert [frame["data"] for frame in output_frames] == [payload]


async def test_transport_round_trips_skip_validate_with_exact_snake_case_key(
    tmp_path: Path,
) -> None:
    """The WS settings patch reaches the runtime and is persisted by the handler."""
    from gui_gateway.handler import GuiRequestHandler
    from gui_gateway.state_store import GuiStateStore
    from gui_gateway.transport import WebSocketTransport

    runtime = _FakeRuntime(str(tmp_path))
    store = GuiStateStore(tmp_path / "gui-state.json")
    handler = GuiRequestHandler(runtime, state_store=store)
    transport = WebSocketTransport(handler)
    ws = _FakeWS(
        [
            json.dumps(
                {
                    "request_id": 41,
                    "method": "settings_set",
                    "params": {"patch": {"skip_validate": True}},
                }
            )
        ]
    )

    await transport.handle(ws)

    response = next(
        json.loads(message) for message in ws.sent if "kind" not in json.loads(message)
    )
    assert response["request_id"] == 41
    assert response["result"]["skip_validate"] is True
    assert runtime.applied_patches == [{"skip_validate": True}]
    assert runtime.settings() == {
        "project_root": str(tmp_path),
        "skip_validate": True,
    }
    assert store.load().skip_validate_for(tmp_path) is True


async def test_transport_resubscribes_after_project_switch() -> None:
    """切换项目后，transport 需重新订阅新 runtime 的事件流。"""
    from gui_gateway.handler import GuiRequestHandler
    from gui_gateway.transport import WebSocketTransport

    target = str(Path.cwd())
    created: list[_FakeRuntime] = []
    old = _FakeRuntime("/old-root")

    def factory(path: str, state_root: Path | None = None) -> _FakeRuntime:
        runtime = _FakeRuntime(path, state_root)
        created.append(runtime)
        return runtime

    handler = GuiRequestHandler(old, factory)
    transport = WebSocketTransport(handler)
    ws = _FakeWS(
        [
            json.dumps(
                {
                    "request_id": 1,
                    "method": "set_project_root",
                    "params": {"path": target},
                }
            )
        ]
    )

    await transport.handle(ws)
    await asyncio.sleep(0)  # flush pending snapshot-emission tasks

    # 连接时（旧 runtime）与切换后（新 runtime）各推一条 state 快照。
    states = [json.loads(m) for m in ws.sent if json.loads(m).get("kind") == "state"]
    assert len(states) >= 2
    assert states[-1]["data"]["project_root"] == target


class _RaisingHandler:
    """Handler double whose ``dispatch`` always raises the configured exception."""

    def __init__(self, exc: Exception) -> None:
        self.runtime = _FakeRuntime("/root")
        self._exc = exc

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise self._exc


async def _dispatch_error(
    exc: Exception, method: str = "session_delete"
) -> dict[str, Any]:
    """用抛 ``exc`` 的 handler 跑一次 dispatch，返回响应里的 error 对象。"""
    from gui_gateway.transport import WebSocketTransport

    transport = WebSocketTransport(_RaisingHandler(exc))
    ws = _FakeWS([json.dumps({"request_id": 7, "method": method, "params": {}})])
    await transport.handle(ws)
    responses = [json.loads(m) for m in ws.sent if "kind" not in json.loads(m)]
    assert len(responses) == 1
    assert responses[0]["request_id"] == 7
    return responses[0]["error"]


async def test_dispatch_error_carries_reason_and_keeps_code() -> None:
    """异常的真实信息回传前端，错误码仍由异常类型决定。"""
    err = await _dispatch_error(ValueError("session 'default' is protected"))

    assert err["code"] == -32602
    assert "session 'default' is protected" in err["message"]
    assert err["data"] == {"exception": "ValueError", "method": "session_delete"}


async def test_dispatch_error_redacts_secrets() -> None:
    """错误文本里的密钥在回传前被脱敏。"""
    err = await _dispatch_error(
        RuntimeError("provider rejected api_key=sk-abcd0123456789 for runtime"),
        method="start",
    )

    assert err["code"] == -32000
    assert "sk-abcd0123456789" not in err["message"]
    assert "[REDACTED]" in err["message"]


async def test_dispatch_error_falls_back_to_exception_type() -> None:
    """异常没有消息文本时，退回异常类型名而不是空串。"""
    err = await _dispatch_error(RuntimeError())

    assert err["message"] == "RuntimeError"


async def test_dispatch_error_reports_a_blocked_delete() -> None:
    """删除会话因句柄占用失败时，前端拿到的是操作系统给的真实原因。"""
    err = await _dispatch_error(
        PermissionError("[WinError 32] the file is in use by another process")
    )

    assert err["code"] == -32603
    assert "in use by another process" in err["message"]
    assert err["data"]["exception"] == "PermissionError"


class _DomainError(Exception):
    def __init__(self) -> None:
        super().__init__("stale revision")
        self.code = "stale_revision"
        self.retryable = True
        self.current_revision = 7


async def test_dispatch_error_preserves_domain_error_metadata() -> None:
    """澄清/确认领域的字符串错误码必须原样带到 WebSocket 响应 data 中。"""
    err = await _dispatch_error(_DomainError(), method="task_clarification_revise")

    assert err["data"]["code"] == "stale_revision"
    assert err["data"]["retryable"] is True
    assert err["data"]["current_revision"] == 7


async def test_dispatch_error_preserves_resume_unavailable_metadata() -> None:
    """A rejected resume remains a typed, non-retryable RPC domain error."""
    from athena.research.runtime.resume_contract import ResearchControlError

    err = await _dispatch_error(
        ResearchControlError(
            "resume_unavailable", "there is no interrupted task to continue"
        ),
        method="resume",
    )

    assert err["data"]["code"] == "resume_unavailable"
    assert err["data"]["retryable"] is False

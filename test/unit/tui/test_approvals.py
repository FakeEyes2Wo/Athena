"""审批测试 — 策略只在 ApprovalCoordinator 一处，闸门永远发问。"""

import asyncio

import pytest

from athena.app_server.protocol import Method, ServerRequest
from athena.core.tool_types import ToolContext
from athena.tui.approvals import ApprovalCoordinator, build_gate
from athena.tui.state import AppState


class FakeSession:
    def __init__(self, verdict: bool = True) -> None:
        self.replies: list[tuple[str, bool]] = []
        self.asked: list[dict] = []
        self.thread_id = "thread-1"
        self.server = self
        self._verdict = verdict

    async def reply_approval(self, server_call_id: str, approved: bool) -> None:
        self.replies.append((server_call_id, approved))

    async def request_approval(
        self, thread_id, turn_id, message, timeout=300.0, payload=None
    ) -> bool:
        self.asked.append(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "message": message,
                "payload": payload,
            }
        )
        return self._verdict


def approval_request(tool: str = "write_file") -> ServerRequest:
    return ServerRequest(
        server_call_id="s1",
        method=Method.ITEM_APPROVAL_REQUEST,
        params={
            "thread_id": "t",
            "turn_id": "turn-1",
            "message": f"{tool}(path=a.py)",
            "tool": tool,
        },
    )


@pytest.fixture
def setup():
    state = AppState(session_id="s")
    session = FakeSession()
    return state, session, ApprovalCoordinator(session, state)


async def test_auto_mode_replies_immediately(setup):
    state, session, coordinator = setup
    state.approval_mode = "auto"
    assert await coordinator.handle(approval_request()) == []
    assert session.replies == [("s1", True)]
    assert state.pending_approval is None


async def test_deny_mode_rejects_with_notice(setup):
    state, session, coordinator = setup
    state.approval_mode = "deny"
    blocks = await coordinator.handle(approval_request())
    assert session.replies == [("s1", False)]
    assert "已拒绝" in blocks[0].payload["text"]


async def test_ask_mode_suspends(setup):
    state, session, coordinator = setup
    assert await coordinator.handle(approval_request()) == []
    assert session.replies == []
    assert state.pending_approval.tool == "write_file"


async def test_always_allow_short_circuits_next_time(setup):
    state, session, coordinator = setup
    await coordinator.handle(approval_request())
    await coordinator.resolve("always")
    assert session.replies == [("s1", True)]
    assert "write_file" in state.always_allow
    await coordinator.handle(approval_request())
    assert session.replies[-1] == ("s1", True)
    assert state.pending_approval is None


async def test_resolve_deny_reports(setup):
    state, session, coordinator = setup
    await coordinator.handle(approval_request())
    blocks = await coordinator.resolve("deny")
    assert session.replies == [("s1", False)]
    assert "已拒绝" in blocks[0].payload["text"]


async def test_resolve_without_pending_is_noop(setup):
    _, session, coordinator = setup
    assert await coordinator.resolve("approve") == []
    assert session.replies == []


async def test_unknown_server_request_warns(setup):
    _, session, coordinator = setup
    request = ServerRequest(server_call_id="s2", method="tool/call/request", params={})
    blocks = await coordinator.handle(request)
    assert blocks[0].payload["level"] == "warn"


async def test_flush_denied_releases_runner(setup):
    state, session, coordinator = setup
    await coordinator.handle(approval_request())
    await coordinator.flush_denied()
    assert session.replies == [("s1", False)]
    assert state.pending_approval is None


async def test_gate_sends_structured_payload():
    session = FakeSession(verdict=True)
    gate = build_gate(session)
    ctx = ToolContext("read_file", "turn-7:call-3", _noop, asyncio.Event())
    assert await gate(ctx, {"path": "a.py"}) is True
    asked = session.asked[0]
    assert asked["turn_id"] == "turn-7"
    assert asked["payload"] == {"tool": "read_file", "args": {"path": "a.py"}}
    assert asked["message"] == "read_file(path=a.py)"


async def test_gate_propagates_rejection():
    session = FakeSession(verdict=False)
    gate = build_gate(session)
    ctx = ToolContext("x", "turn-1:c", _noop, asyncio.Event())
    assert await gate(ctx, {}) is False


async def _noop(kind, ref, data=None) -> None:
    return None

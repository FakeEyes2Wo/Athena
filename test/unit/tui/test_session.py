"""端到端 — MockRunner 经由真实 app_server 走完事件流，再喂给归约与渲染。"""

import asyncio

import pytest

from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.tui.runner import GatedTool, MockRunner, gated_registry
from athena.tui.session import TuiSession
from athena.tui.state import AppState, reduce_event
from athena.tui.theme import ASCII_SYMBOLS, SPINNER_ASCII, Theme
from athena.tui.transcript import Transcript

TIMEOUT = 10.0
TERMINAL = {"turn_completed", "turn_failed", "turn_interrupted"}


class EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="回显输入",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    async def execute(self, input: dict, ctx: ToolContext):
        return input.get("text", "")


@pytest.fixture
async def session():
    created = await TuiSession.create(MockRunner(delay=0.001), session_id="test")
    yield created
    await created.shutdown(timeout=2.0)


async def drain(session, state) -> list:
    """消费事件直到 Turn 终结，返回所有定稿 Block。"""
    blocks = []
    while True:
        event = await asyncio.wait_for(session.next_event(), timeout=TIMEOUT)
        blocks += reduce_event(
            state,
            event.kind,
            event.data,
            event.event_ref,
            event.sequence,
            event.turn_id or "",
        )
        if event.kind in TERMINAL:
            return blocks


async def test_full_turn_produces_renderable_blocks(session):
    state = AppState(session_id="test")
    await session.start_thread()
    turn_id = await session.submit("看看仓库结构")
    assert turn_id

    blocks = await drain(session, state)
    kinds = [b.kind for b in blocks]
    assert kinds[:3] == ["assistant", "tool_begin", "tool_end"]
    assert kinds[-1] == "turn_end"
    assert blocks[-1].payload["status"] == "completed"
    assert state.turn is None and len(state.history) == 1

    theme = Theme(color=False, symbols=ASCII_SYMBOLS, spinner_frames=SPINNER_ASCII)
    rendered = "".join(Transcript(theme, width=80).render(b) for b in blocks)
    assert "list_dir" in rendered and "mock runner" in rendered


async def test_interrupt_marks_turn_interrupted():
    slow = MockRunner(delay=0.2)
    created = await TuiSession.create(slow, session_id="slow")
    state = AppState(session_id="slow")
    try:
        await created.start_thread()
        turn_id = await created.submit("慢一点")
        await asyncio.sleep(0.15)
        await created.interrupt(turn_id, "test")
        blocks = await drain(created, state)
        assert blocks[-1].payload["status"] == "interrupted"
        assert state.history[0].status == "interrupted"
    finally:
        await created.shutdown(timeout=2.0)


async def test_fork_creates_new_thread(session):
    parent = await session.start_thread()
    await session.submit("第一问")
    await drain(session, AppState(session_id="test"))
    child = await session.fork()
    assert child != parent
    assert session.thread_id == child
    assert set(session.threads) == {parent, child}


async def test_switch_moves_subscription(session):
    first = await session.start_thread()
    second = await session.start_thread()
    assert session.thread_id == second
    await session.switch(first)
    assert session.thread_id == first


async def test_submit_without_thread_is_rejected():
    created = await TuiSession.create(MockRunner(delay=0.001), session_id="empty")
    try:
        with pytest.raises(RuntimeError):
            await created.submit("没有线程")
    finally:
        await created.shutdown(timeout=2.0)


async def test_gated_tool_denial_emits_denied_event():
    events = []

    async def emit(kind, ref, data=None):
        events.append((kind, data))

    async def gate(ctx, args):
        return False

    tool = GatedTool(EchoTool(), gate)
    ctx = ToolContext("echo", "turn-1:c1", emit, asyncio.Event())
    result = await tool.ainvoke(ctx, text="hi")
    assert isinstance(result, ToolResult) and result.success is False
    assert [kind for kind, _ in events] == ["tool/begin", "tool/denied"]
    assert events[0][1]["args"] == {"text": "hi"}


async def test_gated_tool_pass_through_emits_real_lifecycle():
    events = []

    async def emit(kind, ref, data=None):
        events.append((kind, data))

    async def gate(ctx, args):
        return True

    tool = GatedTool(EchoTool(), gate)
    ctx = ToolContext("echo", "turn-1:c1", emit, asyncio.Event())
    result = await tool.ainvoke(ctx, text="hi")
    assert result.data == "hi"
    assert [kind for kind, _ in events] == ["tool/begin", "tool/end"]
    assert events[1][1]["preview"] == "hi"
    assert events[1][1]["ok"] is True


async def test_gated_registry_preserves_specs():
    async def gate(ctx, args):
        return True

    source = ToolRegistry()
    source.register(EchoTool())
    wrapped = gated_registry(source, gate)
    assert [s.name for s in wrapped.specs] == ["echo"]
    assert isinstance(wrapped.resolve("echo"), GatedTool)

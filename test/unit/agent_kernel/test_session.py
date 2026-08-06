import asyncio

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.core.agent_kernel.session import (
    AgentSession,
    InMemoryResourcesFactory,
    RunSession,
)
from athena.core.agent_kernel.store import AgentGraphStore
from athena.core.agent_kernel.types import AgentMessage, AgentSpec

from ._support import EchoRunner, JsonCodec


def _spec() -> AgentSpec:
    return AgentSpec(runner=EchoRunner(), codec=JsonCodec(), role="debater")


def _session(store: AgentGraphStore) -> AgentSession:
    resources = InMemoryResourcesFactory().create("root", _spec())
    return AgentSession(agent_id="root", spec=_spec(), resources=resources, store=store)


async def _collect(source, n):
    """从订阅式事件流收集恰好 n 条后终止（事件流为订阅式，不会自然结束）。"""
    events = []
    async for event in source:
        events.append(event)
        if len(events) >= n:
            break
    return events


def test_receive_messages_returns_uncommitted_window() -> None:
    store = AgentGraphStore()
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="a", sequence=1),
        },
    )
    store.commit(
        command_id="m2",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="b", sequence=2),
        },
    )
    session = _session(store)
    assert [m.content for m in session.receive_messages()] == ["a", "b"]
    session.set_active_run("r1", 1)
    session.checkpoint()
    assert session.receive_messages() == []
    store.commit(
        command_id="m3",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="c", sequence=3),
        },
    )
    assert [m.content for m in session.receive_messages()] == ["c"]


def test_event_append_guarded_by_active_run() -> None:
    store = AgentGraphStore()
    session = _session(store)

    async def scenario() -> None:
        await session.append_event("r1", 1, "agent/text_delta", "event://1")
        session.set_active_run("r1", 1)
        await session.append_event("r1", 1, "agent/text_delta", "event://2")
        session.clear_active_run()
        await session.append_event("r1", 1, "agent/text_delta", "event://3")
        events = await _collect(session.events(after_sequence=0), 1)
        assert [e.event_ref for e in events] == ["event://2"]

    asyncio.run(scenario())


def test_run_events_filters_to_one_run() -> None:
    store = AgentGraphStore()
    session = _session(store)

    async def scenario() -> None:
        session.set_active_run("r1", 1)
        await session.append_event("r1", 1, "agent/step", "event://r1-1")
        await session.append_event("r1", 1, "agent/step", "event://r1-2")
        session.clear_active_run()
        session.set_active_run("r2", 1)
        await session.append_event("r2", 1, "agent/step", "event://r2-1")
        run1 = await _collect(session.run_events("r1", after_sequence=0), 2)
        run2 = await _collect(session.run_events("r2", after_sequence=0), 1)
        assert [e.event_ref for e in run1] == ["event://r1-1", "event://r1-2"]
        assert [e.event_ref for e in run2] == ["event://r2-1"]

    asyncio.run(scenario())


def test_subscriber_wakes_on_terminal_event_appended_live() -> None:
    store = AgentGraphStore()
    session = _session(store)

    async def scenario() -> None:
        session.set_active_run("r1", 1)
        reader = asyncio.create_task(_collect(session.events(after_sequence=0), 1))
        await asyncio.sleep(0)  # 让 reader 先进入等待
        session.append_terminal_event("r1", "run_completed", "event://done")
        events = await asyncio.wait_for(reader, timeout=2)
        assert [e.event_ref for e in events] == ["event://done"]

    asyncio.run(scenario())


def test_append_message_guarded_after_interrupt() -> None:
    store = AgentGraphStore()
    session = _session(store)
    session.set_active_run("r1", 1)
    session.append_message(
        "r1", 1, ModelRequest(parts=[UserPromptPart(content="keep")])
    )
    session.interrupt()
    session.append_message(
        "r1", 1, ModelRequest(parts=[UserPromptPart(content="drop")])
    )
    assert len(session.memory.items) == 1


def test_rollback_reverts_memory_to_snapshot() -> None:
    store = AgentGraphStore()
    session = _session(store)
    session.set_active_run("r1", 1)
    before = session.memory.snapshot()[0]
    session.append_message("r1", 1, ModelRequest(parts=[UserPromptPart(content="tmp")]))
    session.memory.rollback(before)
    assert session.memory.items == []


def test_checkpoint_does_not_drop_messages_arriving_during_read() -> None:
    store = AgentGraphStore()
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="a", sequence=1),
        },
    )
    session = _session(store)
    assert [m.content for m in session.receive_messages()] == ["a"]
    session.set_active_run("r1", 1)
    # checkpoint 前新消息到达：提交已读位置而非总长度
    store.commit(
        command_id="m2",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="b", sequence=2),
        },
    )
    session.checkpoint()
    assert [m.content for m in session.receive_messages()] == ["b"]


def test_checkpoint_rejected_without_active_run_or_wrong_generation() -> None:
    store = AgentGraphStore()
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="a", sequence=1),
        },
    )
    session = _session(store)
    assert [m.content for m in session.receive_messages()] == ["a"]
    # 无活跃 Run（旧 runner / 中断后）→ checkpoint 被拒绝
    session.checkpoint()
    assert store.mailbox_committed("root") == 0
    session.set_active_run("r1", 2)
    # 过期 generation 提交 → 拒绝
    session.checkpoint(run_id="r1", generation=1)
    assert store.mailbox_committed("root") == 0


def test_old_generation_cannot_write_memory_or_events() -> None:
    store = AgentGraphStore()
    session = _session(store)
    session.set_active_run("r1", 2)
    session.append_message(
        "r1", 2, ModelRequest(parts=[UserPromptPart(content="current")])
    )
    # 旧 generation 的写入被门禁丢弃
    session.append_message(
        "r1", 1, ModelRequest(parts=[UserPromptPart(content="stale")])
    )
    assert [m.parts[0].content for m in session.memory.items] == ["current"]

    async def scenario() -> None:
        await session.append_event("r1", 1, "agent/step", "event://stale")
        await session.append_event("r1", 2, "agent/step", "event://current")
        events = await _collect(session.events(after_sequence=0), 1)
        assert [e.event_ref for e in events] == ["event://current"]

    asyncio.run(scenario())


def test_memory_view_blocks_direct_writes() -> None:
    store = AgentGraphStore()
    session = _session(store)
    with pytest.raises(AttributeError):
        session.memory.append(ModelRequest(parts=[UserPromptPart(content="direct")]))


def test_receive_messages_does_not_redeliver() -> None:
    store = AgentGraphStore()
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="a", sequence=1),
        },
    )
    session = _session(store)
    assert [m.content for m in session.receive_messages()] == ["a"]
    assert session.receive_messages() == []  # 重复读取不重投（B4）


def test_visible_cursor_is_per_run() -> None:
    store = AgentGraphStore()
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="a", sequence=1),
        },
    )
    session = _session(store)
    session.set_active_run("r1", 1)
    session.receive_messages(run_id="r1", generation=1)  # r1 读取但不 checkpoint
    # 新 Run r2 应看到未提交的消息（游标按 Run 隔离，R4）
    session.set_active_run("r2", 1)
    assert [m.content for m in session.receive_messages(run_id="r2", generation=1)] == ["a"]
    # 旧 Run 视图不得读取/推进（active 已变）
    session.set_active_run("r3", 1)
    assert session.receive_messages(run_id="r1", generation=1) == []
    assert store.mailbox_committed("root") == 0


def test_run_session_blocks_stale_checkpoint_and_rollback() -> None:
    store = AgentGraphStore()
    session = _session(store)
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="a", sequence=1),
        },
    )
    session.set_active_run("r1", 2)
    run_session = RunSession(session, "r1", 2)
    run_session.receive_messages()  # visible = 1
    # 旧 generation 视图 checkpoint → 拒绝
    stale = RunSession(session, "r1", 1)
    stale.checkpoint()
    assert store.mailbox_committed("root") == 0
    # runner 视图无 rollback（B5）
    with pytest.raises(AttributeError):
        run_session.memory.rollback(0)

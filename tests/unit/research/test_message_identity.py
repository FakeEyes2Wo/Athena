"""Agent 消息身份：一条消息的所有 delta 与落盘记录共用一个 message_id。"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_tree import ResearchTree
from athena.research.runtime.events import RuntimeEvents
from athena.research.supervisor.events import EventProjector, OutputEvent
from athena.research.supervisor.state import ResearchState


def _runtime_events(tmp_path: Path, published: list[dict[str, Any]]) -> RuntimeEvents:
    """真实 EventProjector + RuntimeEvents；supervisor 只用最小替身满足 state 投影。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    events = RuntimeEvents(
        events=EventProjector(store),
        store=store,
        sessions_dir=tmp_path / "sessions",
    )
    events.attach_supervisor(
        SimpleNamespace(
            state=ResearchState(
                status="RUNNING", phase="SEARCH", search_limit=10, concurrency=1
            ),
            tree=ResearchTree(),
        )
    )

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        if kind == "output":
            published.append(payload)

    events.subscribe(emit)
    return events


def _transcript(tmp_path: Path) -> list[dict[str, Any]]:
    """读回落盘的会话记录。"""
    path = tmp_path / "sessions" / "default.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _agent_text(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只取 agent 正文记录，排除工具调用与工具输出。"""
    return [
        r
        for r in records
        if r.get("source") == "agent"
        and r.get("channel") == "text"
        and not r.get("tool")
    ]


@pytest.mark.asyncio
async def test_agent_deltas_and_flush_record_share_one_message_id(tmp_path) -> None:
    """同一条 agent 消息：两条实时 delta 与最终落盘记录同 id。"""
    published: list[dict[str, Any]] = []
    events = _runtime_events(tmp_path, published)

    await events.project_agent_event(
        "plan-1", "agent/text_delta", "ref", {"delta": "Let me "}
    )
    await events.project_agent_event(
        "plan-1", "agent/text_delta", "ref", {"delta": "inspect the data."}
    )
    # function_call 是文本边界：先把整条 agent 消息落盘，再投影工具调用。
    await events.project_agent_event(
        "plan-1",
        "agent/function_call",
        "ref",
        {"name": "shell_command", "arguments": {"command": "ls"}},
    )

    deltas = _agent_text(published)
    assert [d["text"] for d in deltas] == ["Let me ", "inspect the data."]
    delta_ids = {d["message_id"] for d in deltas}
    assert len(delta_ids) == 1

    flushed = _agent_text(_transcript(tmp_path))
    assert [f["text"] for f in flushed] == ["Let me inspect the data."]
    assert flushed[0]["message_id"] == delta_ids.pop()

    # 非 agent 正文的事件也要有稳定 id（工具调用同样参与前端 upsert 去重）。
    tool_calls = [p for p in published if p.get("tool") == "shell_command"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["message_id"]


@pytest.mark.asyncio
async def test_consecutive_turns_get_distinct_message_ids(tmp_path) -> None:
    """两次连续 turn 是两条消息，message_id 必须不同。"""
    published: list[dict[str, Any]] = []
    events = _runtime_events(tmp_path, published)

    await events.project_agent_event(
        "plan-1", "agent/text_delta", "ref", {"delta": "first turn."}
    )
    await events.project_agent_event("plan-1", "turn_completed", "ref", {})
    await events.project_agent_event(
        "plan-1", "agent/text_delta", "ref", {"delta": "second turn."}
    )
    await events.project_agent_event("plan-1", "turn_completed", "ref", {})

    flushed = _agent_text(_transcript(tmp_path))
    assert [f["text"] for f in flushed] == ["first turn.", "second turn."]
    assert flushed[0]["message_id"] != flushed[1]["message_id"]


@pytest.mark.asyncio
async def test_legacy_records_without_message_id_still_replay(tmp_path) -> None:
    """升级前写下的 transcript 没有 message_id，回放不能报错。"""
    published: list[dict[str, Any]] = []
    events = _runtime_events(tmp_path, published)
    legacy = {
        "type": "output",
        "seq": 3,
        "source": "agent",
        "channel": "text",
        "text": "old message",
        "plan": "plan-1",
        "tool": None,
        "artifact_ref": None,
        "truncated": False,
    }
    path = tmp_path / "sessions" / "default.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    records = events.replay_output_events()

    assert len(records) == 1
    event = OutputEvent.model_validate(records[0])
    assert event.message_id is None
    assert event.text == "old message"
    assert event.seq == 3


@pytest.mark.asyncio
async def test_word_separating_whitespace_delta_reaches_the_live_stream(
    tmp_path,
) -> None:
    """词间的纯空白 delta 不能丢：丢了实时视图里两个词就粘在一起。"""
    published: list[dict[str, Any]] = []
    events = _runtime_events(tmp_path, published)
    sentence = ("The workspace is empty", " ", "and the data is at D:/tmp/data.")

    for delta in sentence:
        await events.project_agent_event(
            "plan-1", "agent/text_delta", "ref", {"delta": delta}
        )
    await events.project_agent_event("plan-1", "turn_completed", "ref", {})

    live = "".join(p["text"] for p in _agent_text(published))
    assert live == "".join(sentence)

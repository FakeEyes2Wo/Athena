"""TUI 断点续传：output 事件持久化 + 重启重放 + 序列号恢复。"""

import json
from pathlib import Path

import pytest

from athena.research.config import SessionConfig
from athena.research.runtime import ResearchRuntime
from athena_tui.app import AthenaApp
from athena_tui.state import HistoryEntry


@pytest.mark.asyncio
async def test_runtime_persists_and_replays_output_events(tmp_path: Path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    await runtime.publish_output(source="agent", channel="text", text="first")
    await runtime.publish_output(source="tool", channel="stdout", text="second")
    await runtime.aclose()

    log_path = tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl"
    assert log_path.is_file()
    lines = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [line["text"] for line in lines] == ["first", "second"]


@pytest.mark.asyncio
async def test_restart_replays_history_and_resumes_sequence(tmp_path: Path) -> None:
    first = ResearchRuntime(project_root=tmp_path)
    await first.publish_output(source="agent", channel="text", text="one")
    await first.publish_output(source="agent", channel="text", text="two")
    await first.aclose()

    # 重启：新 runtime 重放历史，且新事件 seq 从 3 继续（不与历史冲突）。
    restarted = ResearchRuntime(project_root=tmp_path)
    replayed = restarted.replay_output_events()
    assert [record["text"] for record in replayed] == ["one", "two"]

    seen: list[tuple[str, dict]] = []
    restarted.subscribe(lambda kind, payload: seen.append((kind, payload)))
    await restarted.publish_output(source="agent", channel="text", text="three")
    await restarted.aclose()

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert outputs[-1]["text"] == "three"
    assert outputs[-1]["seq"] == 3  # 续接历史，而非重置为 1


@pytest.mark.asyncio
async def test_scoped_output_is_live_but_only_durable_output_replays(
    tmp_path: Path,
) -> None:
    session = SessionConfig(session_id="session-1")
    runtime = ResearchRuntime(project_root=tmp_path, session=session)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    metadata = {
        "session_id": "session-1",
        "scope": "task_understanding",
        "scope_id": "draft-1",
    }
    await runtime.publish_output(
        source="tool",
        channel="text",
        text="ephemeral progress",
        persist=False,
        **metadata,
    )
    await runtime.publish_output(
        source="agent",
        channel="text",
        text="durable summary",
        persist=True,
        **metadata,
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert [event["text"] for event in outputs] == [
        "ephemeral progress",
        "durable summary",
    ]
    assert all(
        (event["session_id"], event["scope"], event["scope_id"])
        == ("session-1", "task_understanding", "draft-1")
        for event in outputs
    )
    await runtime.aclose()

    restarted = ResearchRuntime(project_root=tmp_path, session=session)
    replayed = restarted.replay_output_events()
    assert [record["text"] for record in replayed] == ["durable summary"]
    assert (
        replayed[0]["session_id"],
        replayed[0]["scope"],
        replayed[0]["scope_id"],
    ) == ("session-1", "task_understanding", "draft-1")
    await restarted.publish_output(
        source="agent",
        channel="text",
        text="after restart",
        **metadata,
    )
    after_restart = [
        record
        for record in restarted.replay_output_events()
        if record["type"] == "output"
    ]
    assert [record["seq"] for record in after_restart] == [2, 3]
    await restarted.aclose()


@pytest.mark.asyncio
async def test_scoped_output_persistence_failure_remains_live_only(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path, session=SessionConfig(session_id="session-1")
    )
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))
    log_path = runtime.events._log_path
    original_open = Path.open

    def fail_transcript_open(path, *args, **kwargs):
        if path == log_path:
            raise OSError("transcript unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_transcript_open)
    with caplog.at_level("ERROR"):
        await runtime.publish_output(
            source="agent",
            channel="text",
            text="durable summary",
            session_id="session-1",
            scope="task_understanding",
            scope_id="draft-1",
            persist=True,
        )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert [event["text"] for event in outputs] == ["durable summary"]
    assert runtime.replay_output_events() == []
    assert "failed to persist session record" in caplog.text
    await runtime.aclose()


@pytest.mark.asyncio
async def test_fresh_project_has_empty_replay(tmp_path: Path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    assert runtime.replay_output_events() == []
    await runtime.aclose()


@pytest.mark.asyncio
async def test_user_message_persists_and_interleaves_with_output(
    tmp_path: Path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.persist_user_message("try random forest")
    await runtime.publish_output(source="agent", channel="text", text="working")
    await runtime.aclose()

    restarted = ResearchRuntime(project_root=tmp_path)
    replayed = restarted.replay_output_events()
    assert [record.get("type") for record in replayed] == ["user", "output"]
    assert replayed[0]["text"] == "try random forest"
    await restarted.aclose()


class _ReplayRuntime:
    """TUI 层 stub：带历史重放接口 + 最小 subscribe 契约。"""

    def __init__(self, records):
        self._records = records
        self.callback = None
        self.closed = False

    def replay_output_events(self):
        return self._records

    def subscribe(self, callback):
        self.callback = callback
        callback(
            "state",
            {
                "type": "state",
                "status": "SEARCH",
                "phase": "SEARCH",
                "plans": [],
                "search": {},
                "sota": None,
                "waiting": None,
            },
        )
        return "sub"

    def unsubscribe(self, _sub_id):
        self.callback = None

    async def message(self, _text):
        return "ok"

    async def aclose(self):
        self.closed = True


def test_tui_restores_history_from_replayed_outputs() -> None:
    records = [
        {"type": "user", "seq": 1, "text": "try random forest"},
        {
            "type": "output",
            "seq": 2,
            "source": "agent",
            "channel": "text",
            "text": "first result",
        },
        {
            "type": "output",
            "seq": 3,
            "source": "tool",
            "channel": "stdout",
            "text": "acc=0.83",
        },
    ]
    app = AthenaApp(_ReplayRuntime(records), Path("/tmp"))

    assert app.state.history == (
        HistoryEntry(kind="user", text="try random forest"),
        HistoryEntry(
            kind="runtime", text="first result", source="agent", channel="text"
        ),
        HistoryEntry(kind="runtime", text="acc=0.83", source="tool", channel="stdout"),
    )
    assert app.state.last_output_seq == 3


def test_tui_without_replay_interface_starts_empty() -> None:
    app = AthenaApp(_ReplayRuntime([]), Path("/tmp"))
    assert app.state.history == ()

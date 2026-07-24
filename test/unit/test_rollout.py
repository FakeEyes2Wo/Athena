"""Unit tests for ``athena.memory.rollout``."""

import json
import tempfile
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    UserPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

# ── helpers ─────────────────────────────────────────────────────────────


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── RolloutRecorder ─────────────────────────────────────────────────────


class TestRolloutRecorder:

    async def test_open_creates_file_in_date_hierarchy(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder

        rec = RolloutRecorder(tmp_project)
        path = await rec.open("thread-abc123")
        await rec.close()
        assert path.exists()
        assert ".athena" in str(path)
        assert path.suffix == ".jsonl"

    async def test_record_writes_jsonl_line(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder

        rec = RolloutRecorder(tmp_project)
        await rec.open("t1")
        rec.record(_user("hello"))
        await rec.close()
        lines = rec.path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["seq"] == 0
        assert "msg" in data

    async def test_multiple_records_increment_seq(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder

        rec = RolloutRecorder(tmp_project)
        await rec.open("t2")
        rec.record(_user("a"))
        rec.record(_assistant("b"))
        rec.record(_user("c"))
        await rec.close()
        lines = rec.path.read_text().strip().split("\n")
        assert len(lines) == 3
        seqs = [json.loads(l)["seq"] for l in lines]
        assert seqs == [0, 1, 2]

    async def test_record_compaction_writes_marker(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder

        rec = RolloutRecorder(tmp_project)
        await rec.open("t3")
        rec.record_compaction(5, "summary text")
        await rec.close()
        data = json.loads(rec.path.read_text().strip())
        assert data["type"] == "compaction"
        assert data["version"] == 5
        assert data["summary"] == "summary text"

    async def test_record_before_open_is_noop(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder

        rec = RolloutRecorder(tmp_project)
        rec.record(_user("nope"))  # no fd → silently dropped
        assert rec.path is None

    async def test_open_is_idempotent(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder

        rec = RolloutRecorder(tmp_project)
        p1 = await rec.open("t4")
        p2 = await rec.open("t4")
        rec.record(_user("still same rollout"))
        await rec.close()
        assert p1 == p2
        assert len(p1.read_text().splitlines()) == 1

    async def test_thread_id_cannot_create_nested_paths(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder

        rec = RolloutRecorder(tmp_project)
        path = await rec.open("../bad/name")
        await rec.close()
        assert path.parents[3] == tmp_project / ".athena" / "sessions"
        assert "/" not in path.name
        assert "\\" not in path.name

    async def test_roundtrip_messages_are_valid_json(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        rec = RolloutRecorder(tmp_project)
        await rec.open("t5")
        msg = ModelResponse(
            parts=[
                TextPart(content="result"),
                ToolCallPart(tool_name="bash", args={"cmd": "ls"}),
            ]
        )
        rec.record(msg)
        rec.record(_tool_result("bash", "file1\nfile2"))
        await rec.close()

        # read back and validate
        adapter = ModelMessagesTypeAdapter
        for line in rec.path.read_text().strip().split("\n"):
            data = json.loads(line)
            msgs = adapter.validate_json(json.dumps(data["msg"]))
            assert len(msgs) == 1


# ── helpers ─────────────────────────────────────────────────────────────


def _tool_result(name: str, content: str) -> ModelRequest:
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name=name,
                content=content,
                tool_call_id="c1",
            )
        ]
    )


# ── resume_context ──────────────────────────────────────────────────────


class TestResumeContext:

    async def test_empty_file_yields_empty_context(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder, resume_context

        rec = RolloutRecorder(tmp_project)
        await rec.open("r1")
        await rec.close()
        ctx = await resume_context(rec.path)
        assert ctx.tokens == 0
        assert ctx.items == []

    async def test_replays_messages_after_compaction(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder, resume_context

        rec = RolloutRecorder(tmp_project)
        await rec.open("r2")
        # simulate: early messages → compact → later messages
        rec.record(_user("early question"))
        rec.record(_assistant("early answer"))
        rec.record_compaction(1, "User asked about X, assistant explained Y.")
        rec.record(_user("follow-up"))
        rec.record(_assistant("follow-up answer"))
        await rec.close()

        ctx = await resume_context(rec.path)
        assert ctx.tokens > 0
        # first item should be the compaction summary
        first = ctx.items[0]
        assert isinstance(first, ModelRequest)
        c = getattr(first.parts[0], "content", "")
        assert "HISTORY SUMMARY" in c
        # last item should be the follow-up
        last_c = getattr(ctx.items[-1].parts[0], "content", "")
        assert last_c == "follow-up answer"

    async def test_no_compaction_replays_all(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder, resume_context

        rec = RolloutRecorder(tmp_project)
        await rec.open("r3")
        rec.record(_user("q1"))
        rec.record(_assistant("a1"))
        rec.record(_user("q2"))
        await rec.close()

        ctx = await resume_context(rec.path)
        assert len(ctx.items) == 3

    async def test_uses_latest_compaction_only(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder, resume_context

        rec = RolloutRecorder(tmp_project)
        await rec.open("r4")
        rec.record(_user("q1"))
        rec.record_compaction(1, "first compaction")
        rec.record(_user("q2"))
        rec.record_compaction(2, "second compaction")
        rec.record(_user("q3"))
        await rec.close()

        ctx = await resume_context(rec.path)
        # only the second compaction summary + q3 should remain
        assert len(ctx.items) == 2
        first = getattr(ctx.items[0].parts[0], "content", "")
        assert "second compaction" in first
        last = getattr(ctx.items[1].parts[0], "content", "")
        assert last == "q3"

    async def test_skips_torn_final_jsonl_record(self, tmp_project):
        from athena.memory.rollout import RolloutRecorder, resume_context

        rec = RolloutRecorder(tmp_project)
        await rec.open("r5")
        rec.record(_user("durable"))
        await rec.close()
        with rec.path.open("a", encoding="utf-8") as fd:
            fd.write('{"seq":1,"msg":')

        ctx = await resume_context(rec.path)
        assert len(ctx.items) == 1
        assert ctx.items[0].parts[0].content == "durable"

    async def test_streams_file_instead_of_using_path_read_text(
        self, tmp_project, monkeypatch
    ):
        from athena.memory.rollout import RolloutRecorder, resume_context

        rec = RolloutRecorder(tmp_project)
        await rec.open("r6")
        rec.record(_user("stream me"))
        await rec.close()

        def fail_read_text(*args, **kwargs):
            raise AssertionError("resume_context must stream JSONL")

        monkeypatch.setattr(Path, "read_text", fail_read_text)
        ctx = await resume_context(rec.path)
        assert ctx.items[0].parts[0].content == "stream me"

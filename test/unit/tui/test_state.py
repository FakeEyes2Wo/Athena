"""归约层测试 — 事件进，Block 出，不碰终端。"""

from athena.tui.state import AppState, reduce_event


def make_state(**kwargs) -> AppState:
    return AppState(session_id="s", **kwargs)


def kinds(blocks) -> list[str]:
    return [b.kind for b in blocks]


def test_turn_started_creates_view():
    state = make_state()
    assert reduce_event(state, "turn_started", None, turn_id="t1") == []
    assert state.turn is not None and state.turn.turn_id == "t1"
    assert state.busy


def test_turn_started_keeps_optimistic_view():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    state.turn.prompt = "问题"
    reduce_event(state, "turn_started", None, turn_id="t1")
    assert state.turn.prompt == "问题"


def test_text_delta_prefers_accumulated():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "agent/text_delta", {"delta": "你", "accumulated": "你"})
    reduce_event(state, "agent/text_delta", {"delta": "好", "accumulated": "你好"})
    assert state.turn.text == "你好"


def test_text_delta_falls_back_to_delta():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "agent/text_delta", {"delta": "a"})
    reduce_event(state, "agent/text_delta", {"delta": "b"})
    assert state.turn.text == "ab"


def test_tool_begin_flushes_pending_text():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "agent/text_delta", {"accumulated": "先说一句"})
    blocks = reduce_event(
        state,
        "tool/begin",
        {"tool": "read_file", "call_id": "c1", "args": {"path": "a.py"}},
    )
    assert kinds(blocks) == ["assistant", "tool_begin"]
    assert blocks[0].payload["text"] == "先说一句"
    assert state.turn.text == ""
    assert state.turn.tools[0].name == "read_file"


def test_parallel_same_tool_pairs_by_call_id():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "tool/begin", {"tool": "read_file", "call_id": "c1"})
    reduce_event(state, "tool/begin", {"tool": "read_file", "call_id": "c2"})
    reduce_event(state, "tool/end", {"tool": "read_file", "call_id": "c2", "ok": True})
    statuses = {t.call_id: t.status for t in state.turn.tools}
    assert statuses == {"c1": "running", "c2": "ok"}


def test_tool_name_recovered_from_event_ref():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    blocks = reduce_event(state, "tool/begin", None, "ev:t1:list_dir:begin")
    assert blocks[-1].payload["name"] == "list_dir"


def test_tool_error_marks_failure():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "tool/begin", {"tool": "x", "call_id": "c1"})
    blocks = reduce_event(
        state, "tool/error", {"tool": "x", "call_id": "c1", "error": "boom"}
    )
    assert blocks[0].payload["ok"] is False
    assert state.turn.tools[0].status == "error"


def test_tool_denied_is_rendered_as_denied():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "tool/begin", {"tool": "x", "call_id": "c1"})
    blocks = reduce_event(state, "tool/denied", {"tool": "x", "call_id": "c1"})
    assert blocks[0].payload["denied"] is True


def test_preview_is_clipped():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "tool/begin", {"tool": "x", "call_id": "c1"})
    blocks = reduce_event(
        state, "tool/end", {"tool": "x", "call_id": "c1", "preview": "y" * 5000}
    )
    assert "已截断" in blocks[0].payload["preview"]


def test_turn_completed_archives_and_reports():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    reduce_event(state, "agent/text_delta", {"accumulated": "结论"})
    reduce_event(state, "tool/begin", {"tool": "x", "call_id": "c1"})
    reduce_event(state, "tool/end", {"tool": "x", "call_id": "c1", "ok": True})
    reduce_event(state, "agent/text_delta", {"accumulated": "收尾"})
    blocks = reduce_event(state, "turn_completed", None)
    assert kinds(blocks) == ["assistant", "turn_end"]
    assert blocks[-1].payload["status"] == "completed"
    assert blocks[-1].payload["tools"] == 1
    assert state.turn is None and len(state.history) == 1
    assert not state.busy


def test_failed_turn_carries_exception_type():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    blocks = reduce_event(state, "turn_failed", {"exception_type": "RuntimeError"})
    assert blocks[-1].payload["exception_type"] == "RuntimeError"


def test_interrupted_status_propagates():
    state = make_state()
    reduce_event(state, "turn_started", None, turn_id="t1")
    blocks = reduce_event(state, "turn_interrupted", None)
    assert blocks[-1].payload["status"] == "interrupted"
    assert state.history[0].status == "interrupted"


def test_unknown_kind_needs_verbose():
    state = make_state()
    assert reduce_event(state, "scheduler/tick", {"n": 1}) == []
    state.verbose = True
    assert kinds(reduce_event(state, "scheduler/tick", {"n": 1})) == ["raw"]


def test_events_are_ringed_and_counted():
    state = make_state()
    reduce_event(state, "turn_started", None, sequence=1, turn_id="t1")
    for i in range(3):
        reduce_event(state, "agent/text_delta", {"accumulated": "x"}, sequence=i + 2)
    assert state.turn.events == 4
    assert [k for _, k in state.recent_events][-1] == "agent/text_delta"


def test_approval_mode_cycles():
    state = make_state()
    assert state.cycle_approval_mode() == "auto"
    assert state.cycle_approval_mode() == "deny"
    assert state.cycle_approval_mode() == "ask"

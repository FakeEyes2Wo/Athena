"""Pure TUI state transitions for replaceable state and append-only output."""

import pytest

from athena.research.supervisor.events import OutputEvent, StateEvent
from athena_tui.state import (
    COMPOSER,
    HistoryEntry,
    TuiState,
    append_user_message,
    apply_output,
    apply_snapshot,
    set_history_follow,
)


def snapshot(*, status: str, phase: str, attempts: int) -> StateEvent:
    return StateEvent(
        status=status,
        phase=phase,
        plans=[],
        search={
            "attempts": attempts,
            "limit": 10,
            "successes": 0,
            "concurrency": 4,
        },
        sota=None,
        waiting=None,
    )


def test_state_snapshot_replaces_all_runtime_projection_fields() -> None:
    state = TuiState(
        mode=COMPOSER,
        history=(HistoryEntry(kind="user", text="kept"),),
        status="WAITING",
        phase="PREPARE",
        search={"attempts": 9},
    )

    updated = apply_snapshot(
        state, snapshot(status="RUNNING", phase="SEARCH", attempts=2)
    )

    assert updated.status == "RUNNING"
    assert updated.phase == "SEARCH"
    assert updated.search["attempts"] == 2
    assert updated.history == (HistoryEntry(kind="user", text="kept"),)
    assert updated.mode == COMPOSER


def test_output_appends_once_in_sequence_order() -> None:
    first = OutputEvent(seq=1, source="agent", channel="text", text="hello")
    duplicate = OutputEvent(seq=1, source="agent", channel="text", text="ignored")
    second = OutputEvent(seq=2, source="tool", channel="stderr", text="boom")

    state = apply_output(TuiState(mode=COMPOSER), first)
    state = apply_output(state, duplicate)
    state = apply_output(state, second)

    assert state.history == (
        HistoryEntry(kind="runtime", text="hello", source="agent", channel="text"),
        HistoryEntry(kind="runtime", text="boom", source="tool", channel="stderr"),
    )
    assert state.last_output_seq == 2
    assert state.history_follow_tail is True
    assert state.unseen_output_count == 0


def test_duplicate_output_leaves_state_equal_and_unseen_unchanged() -> None:
    event = OutputEvent(seq=1, source="agent", channel="text", text="hello")
    first = apply_output(TuiState(), event)

    duplicate = apply_output(first, event)

    assert duplicate == first
    assert duplicate.unseen_output_count == 0


def test_append_user_message_is_bounded_and_follow_tail_is_local() -> None:
    state = TuiState()
    for index in range(1005):
        state = append_user_message(state, f"line {index}")

    assert len(state.history) == 1000
    assert state.history[0] == HistoryEntry(kind="user", text="line 5")
    assert set_history_follow(state, False).history_follow_tail is False


def test_output_appends_a_structured_runtime_entry_without_touching_draft() -> None:
    state = TuiState(composer="keep this draft")
    event = OutputEvent(
        seq=1,
        source="agent",
        channel="text",
        text="first result",
        plan="plan-1",
    )

    updated = apply_output(state, event)

    assert updated.history == (
        HistoryEntry(
            kind="runtime",
            text="first result",
            source="agent",
            channel="text",
            plan="plan-1",
        ),
    )
    assert updated.composer == "keep this draft"


def test_user_submission_is_a_distinct_history_entry() -> None:
    updated = append_user_message(TuiState(), "line one\nline two")

    assert updated.history == (HistoryEntry(kind="user", text="line one\nline two"),)


def test_scrolled_view_counts_new_output_until_tail_follow_resumes() -> None:
    state = set_history_follow(TuiState(), False)
    state = apply_output(
        state,
        OutputEvent(seq=1, source="supervisor", channel="text", text="one"),
    )
    state = apply_output(
        state,
        OutputEvent(seq=2, source="tool", channel="stdout", text="two"),
    )

    assert state.history_follow_tail is False
    assert state.unseen_output_count == 2
    assert set_history_follow(state, True).unseen_output_count == 0


def test_history_entry_rejects_inconsistent_kind_and_source() -> None:
    with pytest.raises(ValueError, match="user history has no runtime source"):
        HistoryEntry(kind="user", source="agent", text="invalid")

    with pytest.raises(ValueError, match="runtime history requires source"):
        HistoryEntry(kind="runtime", source=None, text="invalid")

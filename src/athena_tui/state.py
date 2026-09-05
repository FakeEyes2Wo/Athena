"""Immutable local display state for the two-event TUI."""

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from athena.research.supervisor.events import OutputEvent, StateEvent

COMPOSER = "COMPOSER"
CONFIRMATION = "CONFIRMATION"

_IDEATOR_PLAN = re.compile(r"^ideator-(?:\d+-)?([1-9][0-9]*)$")

HistoryKind = Literal["user", "runtime"]
OutputSource = Literal["supervisor", "agent", "tool"]
OutputChannel = Literal["text", "stdout", "stderr", "error"]


@dataclass(frozen=True)
class HistoryEntry:
    """One local display record; never serialized or sent to the runtime."""

    kind: HistoryKind
    text: str
    source: OutputSource | None = None
    channel: OutputChannel = "text"
    plan: str | None = None
    tool: str | None = None
    truncated: bool = False
    ideator_lanes: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "user" and self.source is not None:
            raise ValueError("user history has no runtime source")
        if self.kind == "runtime" and self.source is None:
            raise ValueError("runtime history requires source")


@dataclass(frozen=True)
class TuiState:
    """Immutable rendering state derived from runtime events and local input."""

    project_root: str = "."
    mode: str = COMPOSER
    status: str = "RUNNING"
    phase: str = "PREPARE"
    search: dict[str, Any] | None = None
    sota: dict[str, Any] | None = None
    waiting: dict[str, Any] | None = None
    manual_mode: bool = False
    pending: tuple[dict[str, Any], ...] = ()
    history: tuple[HistoryEntry, ...] = ()
    last_output_seq: int = 0
    history_follow_tail: bool = True
    unseen_output_count: int = 0
    composer: str = ""
    overlay: str | None = None
    last_error: str | None = None


def apply_snapshot(state: TuiState, event: StateEvent) -> TuiState:
    """Replace every runtime-owned field from one complete state event."""
    return replace(
        state,
        status=event.status,
        phase=event.phase,
        search=dict(event.search),
        sota=None if event.sota is None else dict(event.sota),
        waiting=None if event.waiting is None else dict(event.waiting),
        manual_mode=event.manual,
        pending=tuple(dict(h) for h in event.pending),
    )


def apply_output(state: TuiState, event: OutputEvent) -> TuiState:
    """Append one ordered output, coalescing only adjacent LLM text deltas."""
    if event.seq <= state.last_output_seq:
        return state

    plan = event.plan
    ideator_lanes = None
    if isinstance(plan, str) and _IDEATOR_PLAN.fullmatch(plan):
        ideator_lanes = (state.search or {}).get("ideator_lanes") or None

    entry = HistoryEntry(
        kind="runtime",
        text=event.text,
        source=event.source,
        channel=event.channel,
        plan=plan,
        tool=event.tool,
        truncated=event.truncated,
        ideator_lanes=ideator_lanes,
    )
    history = state.history
    if entry.source == "agent" and entry.channel == "text" and entry.tool is None:
        if history:
            last = history[-1]
            if (
                last.kind == "runtime"
                and last.source == "agent"
                and last.channel == "text"
                and last.plan == entry.plan
                and last.tool is None
            ):
                history = (*history[:-1], replace(last, text=last.text + entry.text))
                entry = None
    if entry is not None:
        history = (*history, entry)[-1000:]
    return replace(
        state,
        history=history,
        last_output_seq=event.seq,
        unseen_output_count=(
            state.unseen_output_count
            if state.history_follow_tail
            else state.unseen_output_count + 1
        ),
    )


def append_user_message(state: TuiState, text: str) -> TuiState:
    """Append an immediate local echo of one submitted user message."""
    entry = HistoryEntry(kind="user", text=text)
    return replace(state, history=(*state.history, entry)[-1000:])


def set_history_follow(state: TuiState, follow: bool) -> TuiState:
    """Toggle tail following and clear unseen count when resuming."""
    return replace(
        state,
        history_follow_tail=follow,
        unseen_output_count=0 if follow else state.unseen_output_count,
    )


def set_error(state: TuiState, message: str | None) -> TuiState:
    """Replace the transient local error."""
    return replace(state, last_error=message)


def open_overlay(state: TuiState, text: str) -> TuiState:
    """Open a local informational overlay while retaining composer/history."""
    return replace(state, overlay=text)


def close_overlay(state: TuiState) -> TuiState:
    """Close the current local informational overlay."""
    return replace(state, overlay=None)


def open_confirmation(state: TuiState, text: str) -> TuiState:
    """Enter local confirmation mode with the supplied prompt."""
    return replace(state, mode=CONFIRMATION, overlay=text)


def close_confirmation(state: TuiState) -> TuiState:
    """Return from local confirmation mode to the composer."""
    return replace(state, mode=COMPOSER, overlay=None)

"""纯状态层 — 事件归约。不 import prompt_toolkit / rich，可脱离终端单测。

``reduce_event()`` 是唯一的状态迁移点：吃一条协议事件，改 ``AppState``，
吐出若干条**已定稿**的 ``Block``。定稿的含义是"可以打进 scrollback 且不再改写"。
未定稿的内容（流式文本、运行中的工具）留在 ``AppState`` 里，由底部动态区重绘。
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

APPROVAL_MODES = ("ask", "auto", "deny")

EVENT_RING_SIZE = 200

TERMINAL_KINDS = {
    "turn_completed": "completed",
    "turn_failed": "failed",
    "turn_interrupted": "interrupted",
}

TOOL_END_KINDS = {"tool/end": True, "tool/error": False, "tool/denied": False}

PREVIEW_LIMIT = 600


@dataclass(slots=True)
class Block:
    """一条定稿输出。``kind`` 决定 transcript 用哪个渲染器。"""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCallView:
    call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: Literal["running", "ok", "error"] = "running"
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.monotonic()) - self.started_at


@dataclass(slots=True)
class TurnView:
    turn_id: str
    prompt: str = ""
    status: Literal["running", "completed", "failed", "interrupted"] = "running"
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    text: str = ""
    tools: list[ToolCallView] = field(default_factory=list)
    events: int = 0

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.monotonic()) - self.started_at

    @property
    def running_tools(self) -> list[ToolCallView]:
        return [t for t in self.tools if t.status == "running"]


@dataclass(slots=True)
class PendingApproval:
    """一次等待用户裁决的 ``item/approval/request``。"""

    server_call_id: str
    tool: str
    message: str
    thread_id: str = ""
    turn_id: str = ""


@dataclass
class AppState:
    """整个 TUI 的可变状态。渲染层只读，不写。"""

    session_id: str
    thread_id: str | None = None
    threads: list[str] = field(default_factory=list)
    turn: TurnView | None = None
    history: list[TurnView] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    verbose: bool = False
    approval_mode: str = "ask"
    always_allow: set[str] = field(default_factory=set)
    pending_approval: PendingApproval | None = None
    model: str = ""
    profile: str = ""
    exit_requested: bool = False
    notice: str = ""
    recent_events: deque = field(default_factory=lambda: deque(maxlen=EVENT_RING_SIZE))

    @property
    def busy(self) -> bool:
        return self.turn is not None and self.turn.status == "running"

    def cycle_approval_mode(self) -> str:
        """按 ask → auto → deny → ask 轮换，返回新模式。"""
        idx = APPROVAL_MODES.index(self.approval_mode)
        self.approval_mode = APPROVAL_MODES[(idx + 1) % len(APPROVAL_MODES)]
        return self.approval_mode


def reduce_event(
    state: AppState,
    kind: str,
    data: dict[str, Any] | None,
    event_ref: str = "",
    sequence: int = 0,
    turn_id: str = "",
) -> list[Block]:
    """把一条事件归约进 ``state``，返回需要提交到 scrollback 的块。

    >>> s = AppState(session_id="s")
    >>> reduce_event(s, "turn_started", None)
    []
    >>> _ = reduce_event(s, "agent/text_delta", {"accumulated": "hi"})
    >>> s.turn.text
    'hi'
    """
    data = data or {}
    state.recent_events.append((sequence, kind))

    if kind == "turn_started":
        # 提交时已乐观建过同 id 的 TurnView，此处不覆盖，避免丢掉 prompt 与计数
        tid = turn_id or str(data.get("turn_id", "")) or event_ref
        if state.turn is None or state.turn.turn_id != tid:
            state.turn = TurnView(turn_id=tid)
        state.turn.events += 1
        return []

    if state.turn is not None:
        state.turn.events += 1

    if kind == "agent/text_delta":
        return _reduce_text_delta(state, data)

    if kind == "tool/begin":
        return _reduce_tool_begin(state, data, event_ref)

    if kind in TOOL_END_KINDS:
        return _reduce_tool_end(state, kind, data, event_ref)

    if kind in TERMINAL_KINDS:
        return _reduce_terminal(state, kind, data)

    if kind == "message":
        return [Block("notice", {"text": event_ref or "message", "level": "info"})]

    if state.verbose:
        return [Block("raw", {"kind": kind, "data": data, "sequence": sequence})]
    return []


def _reduce_text_delta(state: AppState, data: dict[str, Any]) -> list[Block]:
    """累积流式文本。优先用 ``accumulated``，缺失时退回逐段拼接。"""
    if state.turn is None:
        state.turn = TurnView(turn_id="")
    accumulated = data.get("accumulated")
    if isinstance(accumulated, str):
        state.turn.text = accumulated
    else:
        state.turn.text += str(data.get("delta", ""))
    return []


def _reduce_tool_begin(
    state: AppState, data: dict[str, Any], event_ref: str
) -> list[Block]:
    """工具开始前先把已流完的助手文本定稿，再提交工具行。"""
    if state.turn is None:
        state.turn = TurnView(turn_id="")
    blocks = _flush_text(state)
    name = data.get("tool") or _tool_name_from_ref(event_ref)
    call_id = str(data.get("call_id") or event_ref)
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    state.turn.tools.append(ToolCallView(call_id=call_id, name=name, args=args))
    blocks.append(Block("tool_begin", {"name": name, "args": args}))
    return blocks


def _reduce_tool_end(
    state: AppState, kind: str, data: dict[str, Any], event_ref: str
) -> list[Block]:
    """配对 ``tool/begin``，计算耗时并提交结果行。"""
    ok = bool(data.get("ok", TOOL_END_KINDS[kind]))
    name = data.get("tool") or _tool_name_from_ref(event_ref)
    call_id = str(data.get("call_id") or "")
    view = _match_tool(state, call_id, name)
    duration_ms = data.get("duration_ms")
    if view is not None:
        view.status = "ok" if ok else "error"
        view.ended_at = time.monotonic()
        if duration_ms is None:
            duration_ms = int(view.elapsed * 1000)
    preview = _clip(data.get("preview", ""), PREVIEW_LIMIT)
    return [
        Block(
            "tool_end",
            {
                "name": name,
                "ok": ok,
                "denied": kind == "tool/denied",
                "error": data.get("error"),
                "preview": preview,
                "truncated": bool(data.get("truncated")),
                "duration_ms": duration_ms,
            },
        )
    ]


def _reduce_terminal(state: AppState, kind: str, data: dict[str, Any]) -> list[Block]:
    """Turn 结束：定稿尾部文本，归档 TurnView，提交统计尾行。"""
    status = TERMINAL_KINDS[kind]
    if state.turn is None:
        return []
    blocks = _flush_text(state)
    turn = state.turn
    turn.status = status
    turn.ended_at = time.monotonic()
    state.history.append(turn)
    state.turn = None
    blocks.append(
        Block(
            "turn_end",
            {
                "status": status,
                "duration_s": turn.elapsed,
                "tools": len(turn.tools),
                "events": turn.events,
                "exception_type": data.get("exception_type"),
            },
        )
    )
    return blocks


def _flush_text(state: AppState) -> list[Block]:
    """把当前累积的助手文本定稿为一个 Block 并清空。"""
    if state.turn is None or not state.turn.text.strip():
        return []
    text = state.turn.text
    state.turn.text = ""
    return [Block("assistant", {"text": text})]


def _match_tool(state: AppState, call_id: str, name: str) -> ToolCallView | None:
    """先按 call_id 精确配对，失败时回退到"最早一个同名 running"。"""
    if state.turn is None:
        return None
    for view in state.turn.tools:
        if call_id and view.call_id == call_id and view.status == "running":
            return view
    for view in state.turn.tools:
        if view.name == name and view.status == "running":
            return view
    return None


def _tool_name_from_ref(event_ref: str) -> str:
    """从 ``ev:{turn_id}:{call_id}:begin`` 形状的 ref 里抠出可读名字。"""
    parts = event_ref.split(":")
    if len(parts) >= 4:
        return parts[-2]
    return parts[-1] if parts else "tool"


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"

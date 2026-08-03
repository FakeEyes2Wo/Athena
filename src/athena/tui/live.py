"""底部动态区 — 未定稿内容的 formatted-text 构造。

这里只产生 ``(style, text)`` 片段，不碰 IO。凡是会原地刷新的东西都在这里：
流式文本、运行中的工具、状态行、提示行、审批框。
一旦内容定稿，就交给 ``transcript.py`` 打进 scrollback，然后从这里消失。
"""

import json

from prompt_toolkit.utils import get_cwidth

from athena.tui.state import AppState
from athena.tui.theme import Theme

MAX_STREAM_LINES = 12
MAX_TOOL_LINES = 4
ARGS_LIMIT = 72

Fragments = list[tuple[str, str]]


def stream_fragments(state: AppState, theme: Theme) -> Fragments:
    """正在流式输出的助手文本 + 运行中的工具行。空闲时为空。"""
    turn = state.turn
    if turn is None:
        return []
    out: Fragments = []
    text = turn.text.rstrip()
    if text:
        lines = text.splitlines()
        hidden = max(0, len(lines) - MAX_STREAM_LINES)
        if hidden:
            out.append(("class:hint", f"  … 上方还有 {hidden} 行，定稿后写入历史\n"))
        for line in lines[-MAX_STREAM_LINES:]:
            out.append(("class:stream", f"  {line}\n"))
    for tool in turn.running_tools[-MAX_TOOL_LINES:]:
        args = _format_args(tool.args)
        out.append(
            (
                "class:tool",
                f"  {theme.symbols.running} {tool.name}({args})"
                f"  {tool.elapsed:.1f}s\n",
            )
        )
    return out


def status_fragments(
    state: AppState, theme: Theme, frame: str, width: int
) -> Fragments:
    """单行状态：左边是当前活动，右边是会话上下文，中间用空格撑开。"""
    left = _activity_text(state, theme, frame)
    right = _context_text(state, theme)
    pad = max(1, width - _visible_len(left) - _visible_len(right))
    style = "class:status.running" if state.busy else "class:status"
    return [(style, left), ("class:status", " " * pad), ("class:status", right)]


def hint_fragments(state: AppState, theme: Theme) -> Fragments:
    """一行操作提示，随状态变化。"""
    if state.pending_approval is not None:
        return [("class:hint", "  等待审批：y 批准 · n 拒绝 · a 始终批准")]
    parts = []
    if state.busy:
        parts.append("Esc 中断")
    else:
        parts.append("/ 查看命令")
        parts.append("@ 补全路径")
    if state.queued:
        parts.append(f"已排队 {len(state.queued)} 条")
    parts.append("Shift+Tab 切换审批模式")
    parts.append("Ctrl+C 退出")
    return [("class:hint", "  " + f" {theme.symbols.bullet} ".join(parts))]


def approval_fragments(state: AppState, theme: Theme, width: int) -> Fragments:
    """审批框。没有待审批项时返回空，容器据此隐藏。"""
    pending = state.pending_approval
    if pending is None:
        return []
    rule = "─" * max(4, min(width, 78) - 12)
    return [
        ("class:approval", f"┌ 需要审批 {rule}\n"),
        ("class:approval", "│ "),
        ("", f"{pending.message}\n"),
        ("class:approval", "│ "),
        ("class:approval.key", "y"),
        ("", " 批准    "),
        ("class:approval.key", "n"),
        ("", " 拒绝    "),
        ("class:approval.key", "a"),
        ("", f" 本会话始终批准 {pending.tool}\n"),
        ("class:approval", "└" + "─" * (len(rule) + 10)),
    ]


def banner(state: AppState, theme: Theme) -> str:
    """启动横幅 —— 走 transcript 打进 scrollback，所以返回纯 Markdown。"""
    return (
        f"# Athena TUI\n\n"
        f"session `{state.session_id}` · profile `{state.profile}` · "
        f"model `{state.model}`\n\n"
        f"输入问题直接开始；`/help` 查看命令与键位。"
    )


def _activity_text(state: AppState, theme: Theme, frame: str) -> str:
    turn = state.turn
    dot = f" {theme.symbols.bullet} "
    if turn is None:
        return f" {theme.symbols.ok} 空闲"
    running = len(turn.running_tools)
    parts = [
        f"{frame} 运行中",
        f"{turn.elapsed:.1f}s",
        f"{theme.symbols.tool} {len(turn.tools)}",
        f"{turn.events} 事件",
    ]
    if running:
        parts.insert(2, f"{running} 个工具执行中")
    return " " + dot.join(parts)


def _context_text(state: AppState, theme: Theme) -> str:
    dot = f" {theme.symbols.bullet} "
    parts = [f"{state.profile}/{state.model}"]
    if state.thread_id:
        parts.append(f"{theme.symbols.branch} {state.thread_id[:8]}")
    parts.append(f"[{state.approval_mode}]")
    if state.verbose:
        parts.append("verbose")
    return dot.join(parts) + " "


def _format_args(args: dict) -> str:
    if not args:
        return ""
    rendered = ", ".join(f"{k}={_clip(v)}" for k, v in args.items())
    return _clip(rendered, ARGS_LIMIT)


def _clip(value, limit: int = 32) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", "⏎")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _visible_len(text: str) -> int:
    """终端显示宽度 —— CJK 占两列，不能用 len()。"""
    return get_cwidth(text)

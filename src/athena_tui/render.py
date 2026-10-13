"""Codex-inspired responsive rendering for the two-event TUI."""

from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.utils import get_cwidth

from athena_tui.state import CONFIRMATION, HistoryEntry, TuiState, _IDEATOR_PLAN

Fragment = tuple[str, str]
Fragments = list[Fragment]

HELP_TEXT = """输入
Enter 发送    Shift+Enter 换行    Ctrl+J 换行

导航
PageUp/PageDown 滚动输出    End 回到最新    Esc 返回

控制
Tab+Shift 自动/手动    /select <id> 选假设
/manual    /auto    /pause    /resume (continue)    /stop    /quit
/cancel <plan_id> <reason> 取消单个 SEARCH 计划"""

_WAITING_REASON_LABELS = {
    "turn_limit_exhausted": "计划轮次已用尽，需要指导",
}

_PHASE_CLASS = {
    "RUNNING": "class:phase.running",
    "WAITING": "class:phase.waiting",
    "COMPLETED": "class:phase.done",
    "STOPPED": "class:phase.stopped",
    "FAILED": "class:phase.stopped",
}

_MAX_IDEATOR_LANES = 3
_MIN_IDEATOR_LANE_WIDTH = 24

# 猫头鹰 logo：取自 athena-gui/public/athena-logo-*.png 网格中的猫头鹰（不含研究树）。
# 每两行像素合成一行半块字符，"." 为透明。身体用终端默认前景色——深色终端里是白的、
# 浅色终端里是黑的，正好对应 logo 的明暗两版；眼睛固定为金色。
_OWL_PIXELS = (
    "W.......W",
    "WW.....WW",
    "WWWWWWWWW",
    "W.YWWWY.W",
    "WWWW.WWWW",
    "WWWWWWWWW",
    ".WWWWWWW.",
    "..W...W..",
)
_OWL_COLORS = {"W": "", "Y": "#d29922"}
_WELCOME_INDENT = " "
_WELCOME_GAP = "   "
_WELCOME_CAPTIONS = (
    ("class:header.brand", "Athena"),
    ("class:status.secondary", "自动研究工作台"),
    ("class:status.secondary", "描述研究任务开始 · ? 帮助"),
)


def _cell_width(text: str) -> int:
    return sum(max(0, get_cwidth(char)) for char in text)


def _plain(fragments: Iterable[Fragment]) -> str:
    return "".join(text for _style, text in fragments)


def _take_cells(
    fragments: list[Fragment], limit: int
) -> tuple[Fragments, list[Fragment]]:
    taken: Fragments = []
    remaining: Fragments = []
    used = 0
    stopped = False

    for style, text in fragments:
        if stopped:
            remaining.append((style, text))
            continue

        split_at = 0
        for index, char in enumerate(text):
            cells = max(0, get_cwidth(char))
            if used and used + cells > limit:
                stopped = True
                break
            if not used and cells > limit:
                # Preserve an indivisible wide glyph rather than corrupt it.
                split_at = index + 1
                used += cells
                stopped = True
                break
            split_at = index + 1
            used += cells
            if used >= limit:
                stopped = True
                break

        if split_at:
            taken.append((style, text[:split_at]))
        if split_at < len(text):
            remaining.append((style, text[split_at:]))

    return taken, remaining


def _wrap_block(
    first_prefix: Fragments,
    continuation_prefix: Fragments,
    body: Fragments,
    width: int,
) -> tuple[StyleAndTextTuples, ...]:
    logical_lines: list[Fragments] = [[]]
    for style, text in body:
        pieces = text.split("\n")
        if pieces and pieces[-1] == "":
            pieces = pieces[:-1]  # 末尾换行的空片段不渲染成多余空行
        for index, piece in enumerate(pieces):
            if piece:
                logical_lines[-1].append((style, piece))
            if index < len(pieces) - 1:
                logical_lines.append([])

    output: list[StyleAndTextTuples] = []
    first_visual_line = True
    unbounded = width <= 0

    for logical_line in logical_lines:
        remaining = logical_line
        emitted_for_logical_line = False
        while remaining or not emitted_for_logical_line:
            prefix = first_prefix if first_visual_line else continuation_prefix
            if unbounded:
                chunk, remaining = remaining, []
            else:
                prefix_width = _cell_width(_plain(prefix))
                available = max(1, width - prefix_width)
                chunk, remaining = _take_cells(remaining, available)

            output.append([*prefix, *chunk])
            first_visual_line = False
            emitted_for_logical_line = True

    return tuple(output)


def _truncate_right(text: str, width: int) -> str:
    if width <= 0 or _cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    head, _rest = _take_cells([("", text)], width - 1)
    return _plain(head) + "…"


def _truncate_middle(text: str, width: int) -> str:
    if width <= 0 or _cell_width(text) <= width:
        return text
    if width <= 3:
        return _truncate_right(text, width)
    left_cells = (width - 1) // 2
    right_cells = width - 1 - left_cells
    left, _ = _take_cells([("", text)], left_cells)
    reversed_right, _ = _take_cells([("", text[::-1])], right_cells)
    return _plain(left) + "…" + _plain(reversed_right)[::-1]


def _history_block(
    entry: HistoryEntry,
    previous: HistoryEntry | None,
) -> tuple[Fragments, Fragments, Fragments]:
    if entry.kind == "user":
        return (
            [("class:history.user.marker", "› ")],
            [("class:history.user.marker", "  ")],
            [("class:history.user", entry.text)],
        )

    if entry.channel == "error":
        return (
            [("class:history.error", "! ")],
            [("class:history.error", "  ")],
            [("class:history.error", entry.text)],
        )

    if entry.source == "tool":
        label = entry.tool or "tool"
        prefix_text = f"▸ {label} · {entry.channel}  "
        suffix = "  [已截断]" if entry.truncated else ""
        return (
            [("class:history.tool", prefix_text)],
            [("class:history.tool", " " * _cell_width(prefix_text))],
            [("class:history.tool", entry.text + suffix)],
        )

    repeat = (
        previous is not None
        and previous.kind == "runtime"
        and previous.source == entry.source
        and previous.channel == "text"
        and entry.channel == "text"
    )
    if entry.source == "supervisor":
        marker_style = "class:history.supervisor.marker"
        body_style = "class:history.supervisor"
        marker = "  " if repeat else "◆ "
    else:
        marker_style = "class:history.agent.marker"
        body_style = "class:history.agent"
        marker = "  " if repeat else "● "
    return (
        [(marker_style, marker)],
        [(marker_style, "  ")],
        [(body_style, entry.text)],
    )


def _ideator_number(entry: HistoryEntry) -> int | None:
    if entry.kind != "runtime" or entry.plan is None:
        return None
    match = _IDEATOR_PLAN.fullmatch(entry.plan)
    return int(match.group(1)) if match else None


def _render_entry_lines(
    entries: list[HistoryEntry], width: int
) -> tuple[StyleAndTextTuples, ...]:
    lines: list[StyleAndTextTuples] = []
    previous: HistoryEntry | None = None
    for entry in entries:
        first, continuation, body = _history_block(entry, previous)
        lines.extend(_wrap_block(first, continuation, body, width))
        previous = entry
    return tuple(lines)


def pad_line(line: StyleAndTextTuples, width: int) -> StyleAndTextTuples:
    """Right-pad a visual line with spaces to exactly ``width`` cells."""
    missing = max(0, width - _cell_width(_plain(line)))
    return [*line, *(([("", " " * missing)]) if missing else [])]


def _render_ideator_lane(
    number: int, entries: list[HistoryEntry], width: int
) -> tuple[StyleAndTextTuples, ...]:
    title = _fit_one_line([("class:history.ideator.title", f"Ideator {number}")], width)
    return (title, *_render_entry_lines(entries, width))


def _render_ideator_board(
    lanes: list[tuple[int, list[HistoryEntry]]], width: int
) -> tuple[StyleAndTextTuples, ...]:
    if not lanes:
        return ()
    lane_count = len(lanes)
    horizontal = (
        width > 0
        and (width - (lane_count - 1)) // lane_count >= _MIN_IDEATOR_LANE_WIDTH
    )
    if not horizontal:
        output: list[StyleAndTextTuples] = []
        for index, (number, entries) in enumerate(lanes):
            if index:
                output.append([])
            output.extend(_render_ideator_lane(number, entries, width))
        return tuple(output)

    content_width = width - (lane_count - 1)
    base, remainder = divmod(content_width, lane_count)
    lane_widths = [base + (index < remainder) for index in range(lane_count)]
    rendered = [
        _render_ideator_lane(number, entries, lane_width)
        for (number, entries), lane_width in zip(lanes, lane_widths)
    ]
    height = max(len(lines) for lines in rendered)
    output = []
    for row in range(height):
        combined: StyleAndTextTuples = []
        for index, (lines, lane_width) in enumerate(zip(rendered, lane_widths)):
            if index:
                combined.append(("class:history.ideator.separator", "│"))
            line = lines[row] if row < len(lines) else []
            combined.extend(pad_line(line, lane_width))
        output.append(combined)
    return tuple(output)


def _owl_fg(symbol: str) -> str:
    color = _OWL_COLORS[symbol]
    return f"fg:{color}" if color else ""


def _owl_cell(top: str, bottom: str) -> Fragment:
    if top == bottom:
        return ("", " ") if top == "." else (_owl_fg(top), "█")
    if bottom == ".":
        return (_owl_fg(top), "▀")
    if top == ".":
        return (_owl_fg(bottom), "▄")
    # 上下异色：默认前景色没法写成背景色，借 reverse 让默认前景铺底、彩色画另半格。
    if _OWL_COLORS[top] == "":
        return (f"reverse bg:{_OWL_COLORS[bottom]}", "▄")
    return (f"reverse bg:{_OWL_COLORS[top]}", "▀")


def _owl_rows() -> tuple[Fragments, ...]:
    pairs = zip(_OWL_PIXELS[::2], _OWL_PIXELS[1::2])
    return tuple(
        [_owl_cell(top, bottom) for top, bottom in zip(upper, lower)]
        for upper, lower in pairs
    )


def render_welcome_lines(width: int) -> tuple[StyleAndTextTuples, ...]:
    """Render the empty-history welcome: owl logo beside the product name."""
    owl_width = _cell_width(_WELCOME_INDENT) + len(_OWL_PIXELS[0])
    if 0 < width < owl_width:
        # 放不下 logo 时只留品牌名，不输出被截断的半只猫头鹰。
        return ([], _fit_one_line([_WELCOME_CAPTIONS[0]], width))
    lines: list[StyleAndTextTuples] = [[]]
    for index, row in enumerate(_owl_rows()):
        line: Fragments = [("", _WELCOME_INDENT), *row]
        if index < len(_WELCOME_CAPTIONS):
            caption = _WELCOME_CAPTIONS[index]
            line = _fit_one_line([*line, ("", _WELCOME_GAP), caption], width)
        lines.append(line)
    return tuple(lines)


def render_history_lines(state: TuiState, width: int) -> tuple[StyleAndTextTuples, ...]:
    """Project entries to immutable visual lines used by scrolling."""
    if not state.history:
        return render_welcome_lines(width)
    output: list[StyleAndTextTuples] = []
    ordinary: list[HistoryEntry] = []
    ideators: dict[int, list[HistoryEntry]] = {}
    lane_count = 0

    def flush_ordinary() -> None:
        if ordinary:
            output.extend(_render_entry_lines(ordinary, width))
            ordinary.clear()

    def flush_board() -> None:
        nonlocal lane_count
        if lane_count <= 1:
            # 单 lane 不分屏：按普通条目渲染，不产生 "Ideator N" 标题栏。
            for entries in ideators.values():
                output.extend(_render_entry_lines(entries, width))
        elif ideators:
            lanes = [
                (number, ideators.get(number, []))
                for number in range(1, lane_count + 1)
            ]
            output.extend(_render_ideator_board(lanes, width))
        ideators.clear()
        lane_count = 0

    for entry in state.history:
        number = _ideator_number(entry)
        if number is None:
            flush_board()
            ordinary.append(entry)
        elif number <= _MAX_IDEATOR_LANES:
            flush_ordinary()
            batch_lanes = entry.ideator_lanes or number
            lane_count = max(lane_count, batch_lanes)
            ideators.setdefault(number, []).append(entry)
    flush_board()
    flush_ordinary()
    return tuple(output)


def render_header(state: TuiState, width: int) -> StyleAndTextTuples:
    """Render one prioritized product/project/phase/status row."""
    brand = ("class:header.brand", "Athena")
    separator = ("", "  ")
    mode_label = "手动" if state.manual_mode else "自动"
    right_text = f"{state.phase} · {state.status} · {mode_label}"
    right = (_PHASE_CLASS.get(state.status, "class:status.primary"), right_text)
    project = state.project_root

    if width <= 0:
        return [brand, separator, ("class:header.path", project), separator, right]

    fixed = _cell_width("Athena") + 4 + _cell_width(right_text)
    if width >= 88:
        path_text = project
    elif width >= 48:
        path_text = _truncate_middle(project, max(1, width - fixed))
    else:
        path_text = Path(project).name if project else ""

    candidate = [brand, separator]
    if path_text:
        candidate.append(("class:header.path", path_text))
    candidate.extend([separator, right])

    if width > 0 and _cell_width(_plain(candidate)) > width:
        candidate = [brand, separator, right]
        if _cell_width(_plain(candidate)) > width:
            right_width = max(1, width - _cell_width("Athena") - 2)
            candidate = [
                brand,
                separator,
                (
                    _PHASE_CLASS.get(state.status, "class:status.primary"),
                    _truncate_right(right_text, right_width),
                ),
            ]
    return candidate


def _format_metric(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _append_if_fits(
    output: Fragments,
    token: Fragment,
    width: int,
    *,
    separator: Fragment = ("class:status.secondary", "  "),
) -> bool:
    candidate = [*output, *([separator] if output else []), token]
    if width > 0 and _cell_width(_plain(candidate)) > width:
        return False
    output[:] = candidate
    return True


def _fit_one_line(fragments: Fragments, width: int) -> StyleAndTextTuples:
    if width <= 0:
        return fragments
    taken, _remaining = _take_cells(fragments, width)
    return taken


def _error_line(message: str, width: int) -> StyleAndTextTuples:
    return _fit_one_line([("class:status.error", f"! {message}")], width)


def _waiting_line(waiting: dict, width: int) -> StyleAndTextTuples:
    reason = waiting.get("reason") or ""
    text = _WAITING_REASON_LABELS.get(reason, reason)
    return _fit_one_line([("class:status.warning", f"等待指导：{text}")], width)


def _unseen_line(count: int, width: int) -> StyleAndTextTuples:
    return _fit_one_line(
        [("class:status.unseen", f"↓ {count} 条新输出 · End 回到最新")], width
    )


def _progress_line(state: TuiState, width: int) -> StyleAndTextTuples:
    output: Fragments = [
        (
            _PHASE_CLASS.get(state.status, "class:status.primary"),
            f"{state.phase} · {state.status}",
        )
    ]
    if width <= 0:
        return output
    if width < 48:
        return output

    search = state.search or {}
    attempts = search.get("attempts")
    limit = search.get("limit")
    successes = search.get("successes")
    workers = search.get("concurrency")
    metric = _format_metric((state.sota or {}).get("metric"))

    optional: list[Fragment] = []
    if attempts is not None and limit is not None:
        optional.append(("class:status.secondary", f"{attempts}/{limit} attempts"))
    if successes is not None:
        optional.append(("class:status.secondary", f"{successes} successful"))
    if width >= 72 and workers is not None:
        optional.append(("class:status.secondary", f"{workers} workers"))
    if metric is not None:
        optional.append(("class:status.secondary", f"SOTA {metric}"))

    for token in optional:
        _append_if_fits(output, token, width)
    return output


def _manual_selection_line(state: TuiState, width: int) -> StyleAndTextTuples:
    """Manual mode awaiting a Human hypothesis selection."""
    ids = " ".join(str(h.get("id", "?")) for h in state.pending)
    text = f"手动模式 · 待选 {len(state.pending)} 个假设 · /select <id>"
    if ids:
        text += f"  [{ids}]"
    return _fit_one_line([("class:status.warning", text)], width)


def render_status(state: TuiState, width: int) -> StyleAndTextTuples:
    """Render prioritized error/waiting/unseen/progress status."""
    if state.last_error:
        return _error_line(state.last_error, width)
    if state.manual_mode and state.status == "WAITING" and state.pending:
        return _manual_selection_line(state, width)
    if state.status == "WAITING" and state.waiting:
        return _waiting_line(state.waiting, width)
    if not state.history_follow_tail and state.unseen_output_count:
        return _unseen_line(state.unseen_output_count, width)
    return _progress_line(state, width)


def _composer_hint(width: int) -> str:
    if width <= 0:
        return "Enter 发送 · Shift+Enter 换行 · Ctrl+J 换行 · ? 帮助"
    if width >= 88:
        hint = "Enter 发送 · Shift+Enter 换行 · Ctrl+J 换行 · ? 帮助"
    elif width >= 72:
        hint = "Enter 发送 · Shift+Enter 换行 · Ctrl+J 换行"
    elif width >= 32:
        hint = "Enter 发送 · Ctrl+J 换行"
    else:
        hint = ""
    if width > 0 and hint and _cell_width(hint) > width:
        hint = ""
    return hint


def join_lines(lines: tuple[StyleAndTextTuples, ...]) -> StyleAndTextTuples:
    """Flatten visual lines into one fragment list separated by newlines."""
    output: StyleAndTextTuples = []
    for index, line in enumerate(lines):
        if index:
            output.append(("", "\n"))
        output.extend(line)
    return output


def render_bottom_pane(state: TuiState, width: int) -> StyleAndTextTuples:
    """Render composer hints, help, or confirmation."""
    if state.mode == CONFIRMATION:
        text = f"{state.overlay or ''}  Y 确认 · N/Esc 取消"
        return join_lines(_wrap_block([], [], [("class:confirmation", text)], width))
    if state.overlay:
        return join_lines(_wrap_block([], [], [("", state.overlay)], width))
    hint = _composer_hint(width)
    if not hint:
        return []
    return [("class:status.secondary", hint)]


def composer_height(text: str, width: int) -> int:
    """Return the draft's visual height clamped to 1..6 rows."""
    body_width = max(1, width - 4)  # frame borders + "› " prompt
    rows = 0
    for logical_line in text.split("\n"):
        cells = _cell_width(logical_line)
        rows += max(1, (cells + body_width - 1) // body_width)
    return max(1, min(6, rows))

"""转录区 — Block 渲染成 ANSI 文本并写入终端 scrollback。

刻意不使用 ``rich.Live``：转录区的每一行一旦写出就不再改写，
这样终端自身的滚动、选中、复制全部可用，退出 TUI 后记录也还在。
需要原地刷新的内容（流式文本、运行中的工具）由 ``live.py`` 负责。
"""

import io
import json
import re
import shutil
import sys
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from athena.tui.state import Block
from athena.tui.theme import Theme

MIN_WIDTH = 40
MAX_PREVIEW_LINES = 8
ARGS_LIMIT = 110

TRAILING_PAD = re.compile(r"[ \t]+((?:\x1b\[[0-9;]*m)*)$", re.MULTILINE)
"""rich 会把块级内容补齐到整行宽，这些尾部空格进 scrollback 后会污染复制结果。"""


class Transcript:
    """Block → ANSI 字符串 → ``writer``。

    ``writer`` 默认是 ``sys.stdout.write``；在 prompt_toolkit 运行期间
    ``patch_stdout(raw=True)`` 会把它替换成能在动态区上方安全打印的代理。
    """

    def __init__(
        self,
        theme: Theme,
        *,
        writer: Callable[[str], Any] | None = None,
        width: int | None = None,
    ) -> None:
        self._theme = theme
        self._writer = writer
        self._fixed_width = width

    def commit(self, blocks: list[Block]) -> None:
        """把若干 Block 依次写入 scrollback。空列表是常态，直接返回。"""
        if not blocks:
            return
        text = "".join(self.render(b) for b in blocks)
        if text:
            self._write(text)

    def render(self, block: Block) -> str:
        """单个 Block 渲染为带 ANSI 转义的字符串（含结尾换行）。"""
        renderer = _RENDERERS.get(block.kind)
        if renderer is None:
            return ""
        console = self._console()
        renderer(self, console, block.payload)
        return TRAILING_PAD.sub(r"\1", console.file.getvalue())  # type: ignore[union-attr]

    def width(self) -> int:
        if self._fixed_width is not None:
            return self._fixed_width
        return max(MIN_WIDTH, shutil.get_terminal_size((100, 24)).columns)

    def _console(self, width: int | None = None) -> Console:
        return Console(
            file=io.StringIO(),
            force_terminal=self._theme.color,
            no_color=not self._theme.color,
            width=width or self.width(),
            soft_wrap=False,
            highlight=False,
            legacy_windows=False,
        )

    def ansi(self, renderable: Any, width: int | None = None) -> str:
        """把任意 rich 可渲染对象转成带 ANSI 的纯字符串。"""
        console = self._console(width)
        console.print(renderable)
        return console.file.getvalue()  # type: ignore[union-attr]

    def _write(self, text: str) -> None:
        if self._writer is not None:
            self._writer(text)
            return
        sys.stdout.write(text)
        sys.stdout.flush()


def _render_user(t: Transcript, console: Console, payload: dict) -> None:
    sym = t._theme.symbols.user
    body = payload.get("text", "")
    console.print(Text(f"{sym} {body}", style=t._theme.rich("user")))
    console.print()


def _render_assistant(t: Transcript, console: Console, payload: dict) -> None:
    """首行带 ``⏺`` 标记，续行缩进两格 —— Markdown 先单独渲染再逐行加前缀。"""
    text = payload.get("text", "").strip()
    if not text:
        return
    body = t.ansi(Markdown(text), width=max(MIN_WIDTH, console.width - 2))
    lines = body.rstrip("\n").split("\n")
    marker = t.ansi(
        Text(t._theme.symbols.assistant, style=t._theme.rich("assistant")), width=8
    ).rstrip("\n")
    rendered = [f"{marker} {lines[0]}"] + [f"  {line}" for line in lines[1:]]
    console.file.write("\n".join(rendered) + "\n\n")  # type: ignore[union-attr]


def _render_tool_begin(t: Transcript, console: Console, payload: dict) -> None:
    sym = t._theme.symbols.tool
    line = Text(f"{sym} ", style=t._theme.rich("tool"))
    line.append(payload.get("name", "tool"), style=t._theme.rich("tool"))
    args = _format_args(payload.get("args") or {})
    if args:
        line.append(f"({args})", style=t._theme.rich("tool_args"))
    console.print(line)


def _render_tool_end(t: Transcript, console: Console, payload: dict) -> None:
    ok = bool(payload.get("ok"))
    sym = t._theme.symbols.ok if ok else t._theme.symbols.fail
    style = t._theme.rich("ok") if ok else t._theme.rich("fail")
    bits = []
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)):
        bits.append(_format_duration(duration / 1000))
    if payload.get("denied"):
        bits.append("已拒绝")
    if payload.get("truncated"):
        bits.append("已截断")
    head = Text(f"  {sym}", style=style)
    if bits:
        head.append(" " + f" {t._theme.symbols.bullet} ".join(bits), style="dim")
    error = payload.get("error")
    if error:
        head.append(f"  {error}", style=t._theme.rich("fail"))
    console.print(head)
    preview = payload.get("preview") or ""
    for line in _preview_lines(preview):
        console.print(Text("    " + line, style=t._theme.rich("dim")))
    console.print()


def _render_turn_end(t: Transcript, console: Console, payload: dict) -> None:
    status = payload.get("status", "completed")
    ok = status == "completed"
    sym = t._theme.symbols.ok if ok else t._theme.symbols.fail
    label = {
        "completed": "完成",
        "failed": "失败",
        "interrupted": "已中断",
    }.get(status, status)
    dot = f" {t._theme.symbols.bullet} "
    parts = [
        label,
        _format_duration(payload.get("duration_s", 0.0)),
        f"{payload.get('tools', 0)} 工具",
        f"{payload.get('events', 0)} 事件",
    ]
    exception_type = payload.get("exception_type")
    if exception_type:
        parts.insert(1, str(exception_type))
    style = t._theme.rich("ok") if ok else t._theme.rich("fail")
    line = Text(f"  {sym} ", style=style)
    line.append(dot.join(parts), style=t._theme.rich("meta"))
    console.print(line)
    if exception_type:
        console.print(
            Text("    完整原因见日志：--debug 会写到 ~/.athena/tui.log", style="dim")
        )
    console.print()


def _render_notice(t: Transcript, console: Console, payload: dict) -> None:
    level = payload.get("level", "info")
    style = {
        "info": t._theme.rich("info"),
        "warn": t._theme.rich("warn"),
        "error": t._theme.rich("fail"),
        "success": t._theme.rich("ok"),
    }.get(level, t._theme.rich("info"))
    console.print(Text(payload.get("text", ""), style=style))


def _render_markdown(t: Transcript, console: Console, payload: dict) -> None:
    console.print(Markdown(payload.get("text", "")))
    console.print()


def _render_raw(t: Transcript, console: Console, payload: dict) -> None:
    seq = payload.get("sequence", 0)
    kind = payload.get("kind", "?")
    data = payload.get("data")
    body = ""
    if data:
        body = " " + _clip(json.dumps(data, ensure_ascii=False, default=str), 160)
    console.print(Text(f"  [{seq}] {kind}{body}", style=t._theme.rich("raw")))


_RENDERERS: dict[str, Callable[[Transcript, Console, dict], None]] = {
    "user": _render_user,
    "assistant": _render_assistant,
    "tool_begin": _render_tool_begin,
    "tool_end": _render_tool_end,
    "turn_end": _render_turn_end,
    "notice": _render_notice,
    "markdown": _render_markdown,
    "raw": _render_raw,
}


def _format_args(args: dict[str, Any]) -> str:
    """把工具参数压成单行 ``k=v, k=v``，超长截断。"""
    if not args:
        return ""
    parts = []
    for key, value in args.items():
        if isinstance(value, str):
            rendered = value
        else:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        parts.append(f"{key}={_clip(rendered, 48)}")
    return _clip(", ".join(parts), ARGS_LIMIT)


def _preview_lines(preview: str) -> list[str]:
    """预览最多 ``MAX_PREVIEW_LINES`` 行，超出部分折叠成一行提示。"""
    if not preview.strip():
        return []
    lines = preview.splitlines()
    if len(lines) <= MAX_PREVIEW_LINES:
        return lines
    hidden = len(lines) - MAX_PREVIEW_LINES
    return lines[:MAX_PREVIEW_LINES] + [f"… 另有 {hidden} 行"]


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60)}s"


def _clip(text: str, limit: int) -> str:
    flat = text.replace("\n", "⏎")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"

"""配色与符号 — 单一取值点，尊重 NO_COLOR 与 ATHENA_TUI_ASCII。"""

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Symbols:
    """转录区使用的前缀符号。ASCII 模式下退化为纯 ASCII。"""

    user: str
    assistant: str
    tool: str
    ok: str
    fail: str
    running: str
    branch: str
    bullet: str


UNICODE_SYMBOLS = Symbols(
    user="›",
    assistant="⏺",
    tool="⚒",
    ok="✔",
    fail="✘",
    running="◐",
    branch="⎇",
    bullet="·",
)

ASCII_SYMBOLS = Symbols(
    user=">",
    assistant="*",
    tool="#",
    ok="+",
    fail="x",
    running="~",
    branch="Y",
    bullet="-",
)

SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"


@dataclass(frozen=True, slots=True)
class Theme:
    """rich 风格名 + prompt_toolkit 风格名的集中定义。

    ``rich_*`` 用于转录区（rich Console），``pt_*`` 用于底部动态区
    （prompt_toolkit FormattedText），两套色彩体系刻意保持同色调。
    """

    color: bool
    symbols: Symbols
    spinner_frames: str

    def rich(self, name: str) -> str:
        """返回 rich 样式字符串；关闭颜色时返回空串（rich 视为无样式）。"""
        return "" if not self.color else _RICH_STYLES.get(name, "")

    def pt(self, name: str) -> str:
        """返回 prompt_toolkit 样式类名；关闭颜色时返回空串。"""
        return "" if not self.color else _PT_STYLES.get(name, "")


_RICH_STYLES = {
    "user": "bold cyan",
    "assistant": "bold green",
    "tool": "bold yellow",
    "tool_args": "dim",
    "ok": "green",
    "fail": "bold red",
    "warn": "yellow",
    "info": "cyan",
    "dim": "dim",
    "meta": "dim italic",
    "panel_border": "cyan",
    "raw": "dim magenta",
}

_PT_STYLES = {
    "stream": "fg:#8fbf8f",
    "tool": "fg:#d7af5f",
    "status": "fg:#5f8787",
    "status_running": "fg:#5fafd7 bold",
    "status_mode": "fg:#af87d7",
    "hint": "fg:#6c6c6c",
    "prompt": "fg:#5fafd7 bold",
    "approval": "fg:#d78700 bold",
    "queued": "fg:#6c6c6c italic",
    "error": "fg:#d75f5f bold",
}


def load_theme() -> Theme:
    """按环境变量决定颜色与符号集。``NO_COLOR`` 与 ``ATHENA_TUI_ASCII`` 独立生效。"""
    color = not os.environ.get("NO_COLOR")
    ascii_only = bool(os.environ.get("ATHENA_TUI_ASCII"))
    return Theme(
        color=color,
        symbols=ASCII_SYMBOLS if ascii_only else UNICODE_SYMBOLS,
        spinner_frames=SPINNER_ASCII if ascii_only else SPINNER_UNICODE,
    )


PT_STYLE_RULES = [
    ("stream", "#8fbf8f"),
    ("tool", "#d7af5f"),
    ("status", "#5f8787"),
    ("status.running", "#5fafd7 bold"),
    ("status.mode", "#af87d7"),
    ("hint", "#6c6c6c"),
    ("prompt", "#5fafd7 bold"),
    ("approval", "#d78700 bold"),
    ("approval.key", "#ffffff bold"),
    ("queued", "#6c6c6c italic"),
    ("error", "#d75f5f bold"),
    ("separator", "#3a3a3a"),
]

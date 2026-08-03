"""补全 — 斜杠命令与 ``@路径``。

``candidates()`` 是纯函数（只依赖标准库），补全规则可以脱离终端单测；
文件末尾的 ``AthenaCompleter`` 只是把它适配成 prompt_toolkit 的 ``Completer``。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from athena.tui.commands import COMMANDS, command_names

SLASH_PATTERN = re.compile(r"^\s*/([a-zA-Z?]*)$")
MAX_PATH_CANDIDATES = 40
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条补全项。``replace_len`` 是要被替换掉的、光标左侧的字符数。"""

    text: str
    replace_len: int
    display: str = ""
    meta: str = ""


def candidates(text: str, cursor: int, *, cwd: Path | None = None) -> list[Candidate]:
    """按光标左侧的内容决定补全哪一类。无可补全时返回空列表。

    >>> [c.text for c in candidates("/thr", 4)]
    ['threads']
    """
    left = text[:cursor]
    slash = SLASH_PATTERN.match(left)
    if slash is not None:
        return _slash_candidates(slash.group(1))
    at_prefix = _at_prefix(left)
    if at_prefix is not None:
        return _path_candidates(at_prefix, cwd or Path.cwd())
    return []


def _slash_candidates(prefix: str) -> list[Candidate]:
    out = []
    for name in command_names():
        if not name.startswith(prefix):
            continue
        command = COMMANDS[name]
        out.append(
            Candidate(
                text=name,
                replace_len=len(prefix),
                display=command.usage,
                meta=command.summary,
            )
        )
    return out


def _at_prefix(left: str) -> str | None:
    """返回光标左侧 ``@`` 之后的路径片段；不在 ``@`` 上下文中返回 None。"""
    at = left.rfind("@")
    if at < 0:
        return None
    if at > 0 and not left[at - 1].isspace():
        return None
    fragment = left[at + 1 :]
    if any(ch.isspace() for ch in fragment):
        return None
    return fragment


def _path_candidates(prefix: str, cwd: Path) -> list[Candidate]:
    """按 ``@`` 后的片段列出同级条目。目录补全后带 ``/``，便于继续下钻。"""
    raw = Path(prefix)
    if prefix.endswith("/") or prefix.endswith("\\"):
        base, stem = cwd / raw, ""
    else:
        base, stem = cwd / raw.parent, raw.name
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=_entry_sort_key):
        if entry.name in SKIP_DIRS or not entry.name.startswith(stem):
            continue
        suffix = "/" if entry.is_dir() else ""
        out.append(
            Candidate(
                text=entry.name + suffix,
                replace_len=len(stem),
                display=entry.name + suffix,
                meta="目录" if entry.is_dir() else _size_hint(entry),
            )
        )
        if len(out) >= MAX_PATH_CANDIDATES:
            break
    return out


def _entry_sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() else 1, path.name.lower())


def _size_hint(path: Path) -> str:
    size = path.stat().st_size
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


class AthenaCompleter(Completer):
    """把 ``candidates()`` 适配成 prompt_toolkit 的补全器。"""

    def __init__(self, cwd: Path | None = None) -> None:
        self._cwd = cwd

    def get_completions(self, document: Document, complete_event):
        for candidate in candidates(
            document.text, document.cursor_position, cwd=self._cwd
        ):
            yield Completion(
                candidate.text,
                start_position=-candidate.replace_len,
                display=candidate.display or candidate.text,
                display_meta=candidate.meta,
            )

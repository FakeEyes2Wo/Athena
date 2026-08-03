"""输入区 — 多行缓冲、跨会话历史、运行中排队。

``Enter`` 提交、``Alt+Enter`` 换行的语义在 ``keys.py`` 里绑定；
这里只负责缓冲区本身和历史文件。
"""

import os
from pathlib import Path

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.history import FileHistory, History, InMemoryHistory

HISTORY_ENV = "ATHENA_TUI_HISTORY"
DEFAULT_HISTORY_PATH = Path.home() / ".athena" / "tui_history"


def load_history() -> History:
    """优先用文件历史；目录不可写时静默退回内存历史，不让 TUI 起不来。"""
    raw = os.environ.get(HISTORY_ENV)
    if raw == "":
        return InMemoryHistory()
    path = Path(raw) if raw else DEFAULT_HISTORY_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        # 家目录只读或路径不可创建 → 退回内存历史
        return InMemoryHistory()
    return FileHistory(str(path))


def create_buffer(completer: Completer, accept) -> Buffer:
    """创建输入缓冲。``multiline=True`` 只是允许缓冲区里存换行。"""
    return Buffer(
        name="composer",
        completer=completer,
        complete_while_typing=True,
        history=load_history(),
        multiline=True,
        accept_handler=accept,
    )


def take(buffer: Buffer) -> str:
    """取走当前输入并清空缓冲区，同时写入历史。"""
    text = buffer.text.strip()
    if text:
        buffer.append_to_history()
    buffer.reset()
    return text

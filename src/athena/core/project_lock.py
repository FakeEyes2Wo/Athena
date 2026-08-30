"""One writer per project directory.

``.athena/state.json`` and ``research_tree.json`` are the run's durable memory,
and nothing stopped two processes from writing them at once: last write wins,
silently.

Real run (2026-08-30): a resumed run was still alive when a second one started
on the same project. The second rebuilt the tree from an older checkpoint and
overwrote it, and three settled experiments -- including a SOTA at PR-AUC
0.8716 -- disappeared from the tree. Both processes kept running normally; the
only trace was that the tree got shorter.

The lock is an OS-level exclusive lock on one byte of ``run.lock``, held for the
process lifetime, so a crashed process never leaves a stale lock behind: the
kernel drops it when the handle closes. The holder's pid goes in a *separate*
``run.pid`` file -- on Windows, writing inside the locked region breaks the
handle -- and is only ever used to make the error message useful.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_NAME = "run.lock"
PID_NAME = "run.pid"


def _lock_exclusive(handle) -> bool:
    """Take a non-blocking exclusive lock on byte 0; False when already held."""
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # 解锁失败无所谓：句柄关闭时内核会释放。
        pass


class ProjectBusyError(RuntimeError):
    """Raised when another process already holds the project's run lock."""


@contextmanager
def project_lock(athena_dir: Path | str) -> Iterator[Path]:
    """Hold the single-writer lock for one project, or raise ``ProjectBusyError``."""
    athena_dir = Path(athena_dir)
    athena_dir.mkdir(parents=True, exist_ok=True)
    path = athena_dir / LOCK_NAME
    pid_path = athena_dir / PID_NAME
    if not path.exists():
        path.write_bytes(b"\0")
    handle = path.open("r+b")
    try:
        if not _lock_exclusive(handle):
            try:
                holder = pid_path.read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            raise ProjectBusyError(
                f"project {athena_dir.parent} is already being run by "
                f"{holder or 'another process'}. Two runs on one project "
                "overwrite each other's state.json and research_tree.json, and "
                "settled experiments disappear. Stop the other run first."
            )
        try:
            pid_path.write_text(f"pid {os.getpid()}", encoding="utf-8")
        except OSError:
            pass  # 只用于报错文案，写不成也不该拦住运行
        yield path
    finally:
        _unlock(handle)
        handle.close()
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass

"""Atomic persistence helpers shared across durable state stores."""

import errno
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_REPLACE_DELAYS = (0.0, 0.02, 0.05, 0.1, 0.2)
_DIRECTORY_SYNC_UNSUPPORTED = frozenset(
    value
    for value in (
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


def _is_retryable_replace_error(error: BaseException) -> bool:
    """Return whether an atomic replacement may be retried safely."""
    if isinstance(error, PermissionError):
        return True
    if not isinstance(error, OSError):
        return False
    if getattr(error, "winerror", None) in (5, 32):
        return True
    return error.errno in (errno.EACCES, errno.EBUSY)


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Replace ``destination`` while retrying transient sharing violations."""
    last_error: OSError | None = None
    for delay in _REPLACE_DELAYS:
        try:
            os.replace(source, destination)
            return
        except BaseException as error:
            if not _is_retryable_replace_error(error):
                raise
            if not isinstance(error, OSError):
                raise
            last_error = error
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def _fsync_parent(parent: Path) -> None:
    """Flush the directory entry on platforms that support directory fsync."""
    if os.name == "nt":
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        if error.errno in _DIRECTORY_SYNC_UNSUPPORTED:
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: str | Path, payload: bytes) -> Path:
    """Atomically write byte ``payload`` to ``path`` and return the target."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, target)
        _fsync_parent(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def atomic_write_text(path: str | Path, payload: str) -> Path:
    """UTF-8 encode and atomically write text ``payload`` to ``path``."""
    return atomic_write_bytes(path, payload.encode("utf-8"))


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """JSON encode and atomically write ``payload`` to ``path``."""
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, encoded)

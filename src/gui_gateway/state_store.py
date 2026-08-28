"""Persistent GUI state: remembers the active project directory.

Only a single root path is stored; no secrets or session payloads. The file
lives outside user projects so switching directories never pollutes them.
"""

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_PATH = Path.home() / ".athena" / "gui_state.json"


@dataclass(frozen=True, slots=True)
class GuiState:
    """The only persisted GUI state: the active project root."""

    active_project_root: str | None = None


def _default_state() -> GuiState:
    return GuiState()


def validate_project_root(path: str | None) -> str | None:
    """Return a usable absolute project root, or ``None`` when unavailable."""
    if not path or not path.strip():
        return None
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return None
    return str(root)


class GuiStateStore:
    """Atomic, corrupt-tolerant read/write for the GUI state file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_STATE_PATH
        self._lock = threading.Lock()

    def load(self) -> GuiState:
        """Read state; corrupted/missing state degrades to the default."""
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _default_state()
        except (OSError, json.JSONDecodeError):
            self._backup_corrupt()
            return _default_state()
        if not isinstance(payload, dict):
            self._backup_corrupt()
            return _default_state()
        raw_root = payload.get("active_project_root")
        if not isinstance(raw_root, str):
            return _default_state()
        return GuiState(active_project_root=validate_project_root(raw_root))

    def save(self, state: GuiState) -> None:
        """Atomically write state, preserving the old file on failure."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(
                {
                    "active_project_root": state.active_project_root,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".gui_state-", dir=self._path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(data)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self._path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise

    def _backup_corrupt(self) -> None:
        """Keep a one-time copy of an unreadable state file for diagnosis."""
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
            os.replace(self._path, backup)
        except OSError:
            pass

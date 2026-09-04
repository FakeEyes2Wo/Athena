"""Persistent GUI state: remembers the active project directory.

Only paths and session ids are stored; no secrets or session payloads. The file
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
    """Persisted GUI state: the active project root plus each root's last session.

    ``last_sessions`` maps a project root to the session id last opened *in that
    root* — a single global id would make every workspace switch land in another
    workspace's session. ``None`` means "never recorded" (upgrade from a state
    file written before the field existed).
    """

    active_project_root: str | None = None
    last_sessions: dict[str, str] | None = None
    skip_validate_by_project: dict[str, bool] | None = None

    def skip_validate_for(self, project_root: str | Path | None) -> bool:
        """Return the persisted skip preference for a resolved project root."""
        key = str(Path(project_root or ".").expanduser().resolve())
        return (self.skip_validate_by_project or {}).get(key, False)


def _default_state() -> GuiState:
    return GuiState()


def _parse_last_sessions(raw: object) -> dict[str, str] | None:
    """解析「工作区 → 上次会话」映射：缺字段返回 None，脏条目逐条丢弃。"""
    if not isinstance(raw, dict):
        return None
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _parse_skip_validate_by_project(raw: object) -> dict[str, bool] | None:
    """Parse project preferences while dropping malformed individual entries."""
    if not isinstance(raw, dict):
        return None
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, bool)
    }


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
        # 活动工作区不可用（被删/被改名）不影响其余字段：last-active 记录要留着，
        # 用户下次打开那个工作区时还得靠它恢复会话。
        root = validate_project_root(raw_root) if isinstance(raw_root, str) else None
        return GuiState(
            active_project_root=root,
            last_sessions=_parse_last_sessions(payload.get("last_sessions")),
            skip_validate_by_project=_parse_skip_validate_by_project(
                payload.get("skip_validate_by_project")
            ),
        )

    def save(self, state: GuiState) -> None:
        """Atomically write state, preserving the old file on failure."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(
                {
                    "active_project_root": state.active_project_root,
                    "last_sessions": state.last_sessions,
                    "skip_validate_by_project": state.skip_validate_by_project,
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

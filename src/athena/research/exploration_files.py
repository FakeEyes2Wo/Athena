"""Durable, human-readable files produced during SEARCH exploration."""

import re
from pathlib import Path

EXPERIMENT_LOG = "EXPERIMENT_LOG.md"
EXPLORATION_NOTE = "EXPLORATION.md"

_LANE_ID = re.compile(r"^ideator-\d+-\d+$")


def prepare_ideator_lane(root: Path, lane_id: str) -> Path:
    """Create and return one lane-owned directory below ``exploration``."""
    if _LANE_ID.fullmatch(lane_id) is None:
        raise ValueError(f"invalid Ideator lane id: {lane_id}")
    lane = root.resolve() / "exploration" / lane_id
    lane.mkdir(parents=True, exist_ok=True)
    return lane


def write_ideator_result(root: Path, lane_id: str, payload: str) -> Path:
    """Write one accepted Ideator batch beside that lane's evidence files."""
    target = prepare_ideator_lane(root, lane_id) / "result.json"
    target.write_text(payload + "\n", encoding="utf-8", newline="")
    return target


def append_experiment_log(root: Path, entry: str) -> Path:
    """Append one framework-authored outcome to a Plan's experiment log."""
    target = root / EXPERIMENT_LOG
    prefix = "" if not target.exists() else "\n"
    with target.open("a", encoding="utf-8", newline="") as handle:
        handle.write(prefix + entry.strip() + "\n")
    return target


def read_exploration_note(root: Path) -> str | None:
    """Read a non-empty Agent-authored exploration note when one exists."""
    target = root / EXPLORATION_NOTE
    if not target.is_file():
        return None
    content = target.read_text(encoding="utf-8").strip()
    return content or None

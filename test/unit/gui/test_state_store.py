"""Tests for the persistent GUI state store."""

import json
from pathlib import Path

from gui_gateway.state_store import GuiState, GuiStateStore, validate_project_root


def test_missing_file_returns_default(tmp_path: Path) -> None:
    store = GuiStateStore(tmp_path / "gui_state.json")

    assert store.load().active_project_root is None


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "gui_state.json"
    store = GuiStateStore(path)

    store.save(GuiState(active_project_root=str(tmp_path)))

    assert store.load().active_project_root == str(tmp_path.resolve())


def test_corrupt_file_falls_back_and_is_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "gui_state.json"
    path.write_text("{not-json", encoding="utf-8")
    store = GuiStateStore(path)

    assert store.load().active_project_root is None
    assert any(path.parent.glob(f"{path.name}.corrupt-*"))


def test_non_dict_payload_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "gui_state.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    assert GuiStateStore(path).load().active_project_root is None


def test_missing_directory_is_not_restored(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert validate_project_root(str(missing)) is None


def test_valid_directory_is_resolved(tmp_path: Path) -> None:
    assert validate_project_root(str(tmp_path)) == str(tmp_path.resolve())

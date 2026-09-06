"""Tests for the persistent GUI state store."""

import json
from pathlib import Path

from gui_gateway.state_store import GuiState, GuiStateStore, validate_project_root


def test_missing_file_returns_default(tmp_path: Path) -> None:
    store = GuiStateStore(tmp_path / "gui_state.json")

    assert store.load().active_project_root is None
    assert store.load().skip_validate_for(tmp_path) is False


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "gui_state.json"
    store = GuiStateStore(path)

    store.save(GuiState(active_project_root=str(tmp_path)))

    assert store.load().active_project_root == str(tmp_path.resolve())


def test_skip_validate_preferences_round_trip_and_are_project_scoped(tmp_path) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    store = GuiStateStore(tmp_path / "gui_state.json")
    store.save(
        GuiState(
            active_project_root=str(project_a),
            last_sessions={str(project_a.resolve()): "s-1"},
            skip_validate_by_project={str(project_a.resolve()): True},
        )
    )

    restored = store.load()

    assert restored.skip_validate_for(project_a) is True
    assert restored.skip_validate_for(project_b) is False
    assert restored.last_sessions == {str(project_a.resolve()): "s-1"}


def test_malformed_skip_validate_entries_are_dropped(tmp_path) -> None:
    path = tmp_path / "gui_state.json"
    path.write_text(
        json.dumps(
            {
                "skip_validate_by_project": {
                    str(tmp_path.resolve()): True,
                    "/bad/int": 1,
                    "/bad/string": "true",
                }
            }
        ),
        encoding="utf-8",
    )

    restored = GuiStateStore(path).load()

    assert restored.skip_validate_by_project == {str(tmp_path.resolve()): True}


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
    assert GuiStateStore(path).load().skip_validate_for(tmp_path) is False


def test_missing_directory_is_not_restored(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert validate_project_root(str(missing)) is None


def test_valid_directory_is_resolved(tmp_path: Path) -> None:
    assert validate_project_root(str(tmp_path)) == str(tmp_path.resolve())


def test_missing_last_sessions_field_is_none(tmp_path: Path) -> None:
    """旧版状态文件没有 last_sessions 字段：按 None 处理，而不是报错。"""
    path = tmp_path / "gui_state.json"
    path.write_text(
        json.dumps({"active_project_root": str(tmp_path)}), encoding="utf-8"
    )

    assert GuiStateStore(path).load().last_sessions is None
    assert GuiStateStore(path).load().skip_validate_for(tmp_path) is False


def test_last_sessions_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "gui_state.json"
    store = GuiStateStore(path)

    store.save(
        GuiState(active_project_root=str(tmp_path), last_sessions={"/work/a": "s-1"})
    )

    assert store.load().last_sessions == {"/work/a": "s-1"}


def test_corrupt_file_degrades_last_sessions_to_default(tmp_path: Path) -> None:
    path = tmp_path / "gui_state.json"
    path.write_text('{"last_sessions": {"/work/a": "s-1"', encoding="utf-8")

    assert GuiStateStore(path).load().last_sessions is None
    assert GuiStateStore(path).load().skip_validate_for(tmp_path) is False


def test_non_string_last_session_entries_are_dropped(tmp_path: Path) -> None:
    """脏条目逐条丢弃，不牵连同一份文件里其它可用的映射。"""
    path = tmp_path / "gui_state.json"
    path.write_text(
        json.dumps({"last_sessions": {"/work/a": "s-1", "/work/b": 7}}),
        encoding="utf-8",
    )

    assert GuiStateStore(path).load().last_sessions == {"/work/a": "s-1"}


def test_last_sessions_survive_an_unusable_project_root(tmp_path: Path) -> None:
    """活动工作区已被删除时仍要保住每个工作区的 last-active 记录。"""
    path = tmp_path / "gui_state.json"
    path.write_text(
        json.dumps(
            {
                "active_project_root": str(tmp_path / "gone"),
                "last_sessions": {"/work/a": "s-1"},
            }
        ),
        encoding="utf-8",
    )

    state = GuiStateStore(path).load()

    assert state.active_project_root is None
    assert state.last_sessions == {"/work/a": "s-1"}

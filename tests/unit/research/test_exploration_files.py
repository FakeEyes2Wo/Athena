"""Tests for durable SEARCH exploration files."""

import json

import pytest

from athena.research.exploration_files import (
    append_experiment_log,
    prepare_ideator_lane,
    read_exploration_note,
    write_ideator_result,
)


def test_prepare_ideator_lane_creates_lane_owned_path(tmp_path) -> None:
    lane = prepare_ideator_lane(tmp_path, "ideator-2-3")

    assert lane == tmp_path.resolve() / "exploration" / "ideator-2-3"
    assert lane.is_dir()


@pytest.mark.parametrize(
    "lane_id",
    ["ideator-2", "ideator-two-3", "../ideator-2-3", "ideator-2-3/notes"],
)
def test_prepare_ideator_lane_rejects_invalid_id(tmp_path, lane_id: str) -> None:
    with pytest.raises(ValueError, match="invalid Ideator lane id"):
        prepare_ideator_lane(tmp_path, lane_id)


def test_write_ideator_result_writes_json_in_lane(tmp_path) -> None:
    payload = json.dumps({"hypotheses": [{"statement": "try grouped folds"}]})

    target = write_ideator_result(tmp_path, "ideator-1-2", payload)

    assert target == (
        tmp_path.resolve() / "exploration" / "ideator-1-2" / "result.json"
    )
    assert target.read_text(encoding="utf-8") == payload + "\n"


def test_append_experiment_log_keeps_prior_entries(tmp_path) -> None:
    target = append_experiment_log(tmp_path, "## Turn 1\nscore: 0.40")
    append_experiment_log(tmp_path, "  ## Turn 2\nstatus: failed  ")

    assert target == tmp_path / "EXPERIMENT_LOG.md"
    assert target.read_text(encoding="utf-8") == (
        "## Turn 1\nscore: 0.40\n\n## Turn 2\nstatus: failed\n"
    )


def test_read_exploration_note_is_optional(tmp_path) -> None:
    assert read_exploration_note(tmp_path) is None

    note = tmp_path / "EXPLORATION.md"
    note.write_text("  \n", encoding="utf-8")
    assert read_exploration_note(tmp_path) is None

    note.write_text("\n  promising categorical interaction  \n", encoding="utf-8")
    assert read_exploration_note(tmp_path) == "promising categorical interaction"

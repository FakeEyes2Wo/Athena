"""A prediction file that answers the wrong rows must not become a score.

The trusted evaluator joins on ``__athena_row_id`` and reports what the join
produced. Nothing about a wrong answer looks wrong from the outside: the file
parses, the ids are integers, the row count can match exactly, and the score
comes back as an ordinary float.
"""

from pathlib import Path

import pytest

from athena.research.predictions_cover import (
    PredictionsCoverageError,
    assert_predictions_cover,
    row_ids,
)


def _csv(path: Path, ids, header: str = "__athena_row_id,label") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{row_id},0" for row_id in ids)
    path.write_text(f"{header}\n{body}\n", encoding="utf-8")
    return path


def test_positional_indices_written_as_row_ids_are_rejected(tmp_path) -> None:
    """The 2026-09-02 PREPARE baseline, in miniature.

    It wrote ``0, 1, 2, ...`` against labels spanning ``304 … 846890``. 36,674 of
    169,725 label rows found a match, every one pairing a label with some other
    window's prediction. The agent measured PR-AUC 0.7982 for itself; the trusted
    evaluator returned 0.016331, and that became the reference metric for the
    whole run -- which does not fail a run, it silently re-scales it.
    """
    expected = _csv(tmp_path / "features.csv", range(60, 160))
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", range(100))

    with pytest.raises(PredictionsCoverageError) as excinfo:
        assert_predictions_cover(predictions, expected)

    detail = str(excinfo.value)
    # 部分重叠正是危险所在：分数看上去像个正常的数。
    assert "60 of 100 rows" in detail
    assert "overlap 40" in detail
    assert "features.csv" in detail
    assert "positional indices" in detail


def test_predictions_for_a_different_split_say_so(tmp_path) -> None:
    expected = _csv(tmp_path / "final_features.csv", range(500, 600))
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", range(100))

    with pytest.raises(PredictionsCoverageError) as excinfo:
        assert_predictions_cover(predictions, expected)

    assert "a different split" in str(excinfo.value)
    assert "ATHENA_PREDICT_FEATURES" in str(excinfo.value)


def test_full_coverage_passes_regardless_of_order_or_extra_rows(tmp_path) -> None:
    expected = _csv(tmp_path / "features.csv", range(100))
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", list(reversed(range(150))))

    assert_predictions_cover(predictions, expected)


def test_coverage_can_be_spread_across_several_prediction_files(tmp_path) -> None:
    expected = _csv(tmp_path / "features.csv", range(100))
    predictions = tmp_path / "predictions"
    _csv(predictions / "a.csv", range(50))
    _csv(predictions / "nested" / "b.csv", range(50, 100))

    assert_predictions_cover(predictions, expected)


@pytest.mark.parametrize("side", ["expected", "produced"])
def test_a_task_without_row_ids_is_left_alone(tmp_path, side: str) -> None:
    """Not every task is tabular; without ids there is nothing to check."""
    expected = _csv(
        tmp_path / "features.csv",
        range(100),
        header="row,label" if side == "expected" else "__athena_row_id,label",
    )
    predictions = tmp_path / "predictions"
    _csv(
        predictions / "predictions.csv",
        range(500, 600),
        header="row,label" if side == "produced" else "__athena_row_id,label",
    )

    assert_predictions_cover(predictions, expected)


def test_row_ids_are_read_past_a_utf8_bom_and_stripped(tmp_path) -> None:
    path = tmp_path / "features.csv"
    path.write_text(
        "﻿__athena_row_id,label\n 65220 ,0\n65221,1\n", encoding="utf-8"
    )

    assert row_ids(path) == {"65220", "65221"}

"""A prediction file that answers the wrong rows must not become a score.

The trusted evaluator joins predictions to labels on the split's identity column
and reports what the join produced. Nothing about a wrong answer looks wrong from
the outside: the file parses, the ids are integers, the row count can match
exactly, and the score comes back as an ordinary float.
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


def test_full_coverage_passes_regardless_of_order(tmp_path) -> None:
    expected = _csv(tmp_path / "features.csv", range(100))
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", list(reversed(range(100))))

    assert_predictions_cover(predictions, expected)


def test_coverage_can_be_spread_across_several_prediction_files(tmp_path) -> None:
    expected = _csv(tmp_path / "features.csv", range(100))
    predictions = tmp_path / "predictions"
    _csv(predictions / "a.csv", range(50))
    _csv(predictions / "nested" / "b.csv", range(50, 100))

    assert_predictions_cover(predictions, expected)


def test_rows_the_split_does_not_contain_are_rejected(tmp_path) -> None:
    """Full coverage plus inventions is still the wrong answer sheet."""
    expected = _csv(tmp_path / "features.csv", range(100))
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", range(150))

    with pytest.raises(PredictionsCoverageError, match="absent from"):
        assert_predictions_cover(predictions, expected)


def test_a_repeated_prediction_row_is_rejected(tmp_path) -> None:
    expected = _csv(tmp_path / "features.csv", range(3))
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", [0, 1, 2, 2])

    with pytest.raises(PredictionsCoverageError, match="duplicate identity"):
        assert_predictions_cover(predictions, expected)


def test_a_repeated_expected_row_is_reported_against_the_feature_file(
    tmp_path,
) -> None:
    expected = _csv(tmp_path / "features.csv", [0, 1, 1])
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", range(2))

    with pytest.raises(PredictionsCoverageError, match="features.csv has duplicate"):
        assert_predictions_cover(predictions, expected)


def test_expected_data_without_identity_values_is_left_alone(tmp_path) -> None:
    """Not every task is tabular; without expected ids there is nothing to check."""
    expected = tmp_path / "features.csv"
    expected.write_text("__athena_row_id,label\n", encoding="utf-8")
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", range(500, 600))

    assert_predictions_cover(predictions, expected)


def test_the_identity_column_is_taken_by_position_not_by_name(tmp_path) -> None:
    """A dataset whose public contract calls it ``image_filename`` still checks out."""
    expected = _csv(tmp_path / "features.csv", range(3), header="image_filename,f")
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", range(3), header="image_filename,score")

    assert_predictions_cover(predictions, expected)


def test_row_ids_are_read_past_a_utf8_bom_and_stripped(tmp_path) -> None:
    path = tmp_path / "features.csv"
    path.write_text("﻿__athena_row_id,label\n 65220 ,0\n65221,1\n", encoding="utf-8")

    values, duplicates = row_ids(path)

    assert values == ["65220", "65221"]
    assert duplicates == set()


def test_a_coverage_failure_is_still_a_value_error(tmp_path) -> None:
    """Callers that predate the typed error keep catching it."""
    expected = _csv(tmp_path / "features.csv", range(500, 600))
    predictions = tmp_path / "predictions"
    _csv(predictions / "predictions.csv", range(100))

    with pytest.raises(ValueError):
        assert_predictions_cover(predictions, expected)

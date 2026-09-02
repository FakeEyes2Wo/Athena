"""Focused tests for the dataset-neutral evaluator directory contract."""

import json
from pathlib import Path

import pytest

from athena.research.evaluation.spec import (
    EvaluatorSpec,
    load_evaluator_spec,
    prediction_filename,
)


def test_evaluator_spec_accepts_custom_identity_and_probability_columns() -> None:
    """Future datasets can choose their own id and probability field names."""
    spec = EvaluatorSpec(
        task_id="record-classification",
        prediction_file="predictions__record-classification.csv",
        prediction_id_column="record_key",
        prediction_column="predicted_label",
        probability_columns=("prob_negative", "prob_positive"),
    )

    assert spec.prediction_id_column == "record_key"
    assert spec.probability_columns == ["prob_negative", "prob_positive"]


def test_evaluator_spec_rejects_mismatched_standard_prediction_filename() -> None:
    """A tabular task cannot silently drift from the submission filename contract."""
    with pytest.raises(ValueError, match="prediction_file"):
        EvaluatorSpec(
            task_id="task-a",
            prediction_file="predictions.csv",
            prediction_id_column="id",
            prediction_column="prediction",
        )


def test_load_evaluator_spec_preserves_legacy_metric_json(tmp_path: Path) -> None:
    """Old evaluator checkpoints remain readable during the migration window."""
    (tmp_path / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )

    assert load_evaluator_spec(tmp_path) is None


def test_prediction_filename_rejects_path_injection() -> None:
    """Task ids cannot cause evaluator output to escape its directory."""
    with pytest.raises(ValueError):
        prediction_filename("../labels")

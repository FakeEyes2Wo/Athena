"""Focused tests for the dataset-neutral evaluator directory contract."""

import json
from pathlib import Path

import pytest

from athena.research.evaluation.spec import (
    EvaluatorSpec,
    load_evaluator_spec,
    parse_evaluator_spec,
)


def test_evaluator_spec_accepts_custom_identity_and_probability_columns() -> None:
    """Future datasets can choose their own id and probability field names."""
    spec = EvaluatorSpec(
        contract_version=2,
        task_id="record-classification",
        task_type="classification",
        primary_metric="macro_f1",
        class_labels=("negative", "positive"),
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
            contract_version=2,
            task_id="task-a",
            task_type="regression",
            primary_metric="rmse",
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


def test_load_evaluator_spec_rejects_partial_v2_instead_of_legacy_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "metric.json").write_text(
        json.dumps(
            {
                "contract_version": 2,
                "task_type": "classification",
                "prediction_id_column": "sample_key",
                "eval_script": "evaluate.py",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="partially specified.*task_id"):
        load_evaluator_spec(tmp_path)


def test_legacy_prediction_format_does_not_count_as_partial_v2(
    tmp_path: Path,
) -> None:
    (tmp_path / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py", "prediction_format": "custom"}),
        encoding="utf-8",
    )
    assert load_evaluator_spec(tmp_path) is None


def test_parse_evaluator_spec_preserves_legacy_and_partial_v2_rules() -> None:
    """The in-memory parser keeps the loader's migration behavior."""
    assert (
        parse_evaluator_spec(
            {"eval_script": "evaluate.py", "prediction_format": "custom"}
        )
        is None
    )
    with pytest.raises(ValueError, match="partially specified.*task_id"):
        parse_evaluator_spec(
            {"contract_version": 2, "prediction_id_column": "sample_key"}
        )


def test_classification_contract_requires_the_complete_label_universe() -> None:
    """Macro metrics cannot silently infer a different class set per split."""
    with pytest.raises(ValueError, match="complete class_labels"):
        EvaluatorSpec(
            contract_version=2,
            task_id="classification",
            task_type="classification",
            primary_metric="macro_f1",
            prediction_file="predictions__classification.csv",
            prediction_id_column="id",
            prediction_column="label",
        )


def test_evaluator_spec_rejects_task_id_path_injection() -> None:
    """Task ids cannot cause evaluator output to escape its directory."""
    with pytest.raises(ValueError):
        EvaluatorSpec(
            contract_version=2,
            task_id="../labels",
            task_type="regression",
            primary_metric="rmse",
            prediction_file="predictions__../labels.csv",
            prediction_id_column="id",
            prediction_column="prediction",
        )

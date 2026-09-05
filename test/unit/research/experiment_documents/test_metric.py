"""Tests for frozen evaluator metric authority and fallback order."""

import json
from pathlib import Path

from athena.research.experiment_documents.projector import _resolve_metric


def _write_metric(root: Path, payload: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "metric.json").write_text(json.dumps(payload), encoding="utf-8")


def _v2_metric(name: str) -> dict[str, object]:
    return {
        "contract_version": 2,
        "task_id": "demo",
        "task_type": "regression",
        "primary_metric": name,
        "class_labels": [],
        "prediction_file": "predictions__demo.csv",
        "prediction_id_column": "__athena_row_id",
        "prediction_column": "prediction",
        "probability_columns": [],
        "metrics_file": "metrics_public_test.csv",
        "eval_script": "eval_metrics.py",
        "prediction_format": "tabular_csv",
    }


def test_frozen_evaluate_metric_wins_over_every_fallback(tmp_path: Path) -> None:
    evaluate = tmp_path / "workspaces" / "evaluator" / "evaluate"
    flat = evaluate.parent
    _write_metric(evaluate, _v2_metric("roc_auc"))
    _write_metric(flat, {"primary_metric": "accuracy"})
    assert _resolve_metric((evaluate, flat), {"primary_metric": "f1"}) == "roc_auc"


def test_legacy_flat_metric_wins_when_evaluate_is_absent(tmp_path: Path) -> None:
    flat = tmp_path / "workspaces" / "evaluator"
    _write_metric(flat, {"primary_metric": "mae"})
    assert (
        _resolve_metric((flat / "evaluate", flat), {"primary_metric": "rmse"}) == "mae"
    )


def test_invalid_evaluator_falls_through_with_diagnostic(
    tmp_path: Path, caplog
) -> None:
    evaluate = tmp_path / "evaluate"
    _write_metric(evaluate, {"task_id": "partial", "primary_metric": "bad"})

    assert _resolve_metric((evaluate,), {"primary_metric": "f1"}) == "f1"
    assert "invalid evaluator metric" in caplog.text


def test_malformed_json_falls_through_with_diagnostic(tmp_path: Path, caplog) -> None:
    evaluate = tmp_path / "evaluate"
    evaluate.mkdir()
    (evaluate / "metric.json").write_text("{", encoding="utf-8")

    assert _resolve_metric((evaluate,), {"primary_metric": "f1"}) == "f1"
    assert "invalid evaluator metric" in caplog.text


def test_non_object_json_falls_through_with_diagnostic(tmp_path: Path, caplog) -> None:
    evaluate = tmp_path / "evaluate"
    evaluate.mkdir()
    (evaluate / "metric.json").write_text("[]", encoding="utf-8")

    assert _resolve_metric((evaluate,), {"primary_metric": "f1"}) == "f1"
    assert "invalid evaluator metric" in caplog.text


def test_blank_legacy_metric_falls_through_to_task_understanding(
    tmp_path: Path,
    caplog,
) -> None:
    evaluate = tmp_path / "evaluate"
    _write_metric(evaluate, {"primary_metric": "  "})

    assert _resolve_metric((evaluate,), {"primary_metric": "f1"}) == "f1"
    assert "invalid evaluator metric" in caplog.text


def test_valid_second_evaluator_wins_after_invalid_first(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid"
    valid = tmp_path / "valid"
    _write_metric(invalid, {"task_id": "partial", "primary_metric": "bad"})
    _write_metric(valid, _v2_metric("balanced_accuracy"))

    assert _resolve_metric((invalid, valid), {"primary_metric": "f1"}) == (
        "balanced_accuracy"
    )


def test_task_understanding_then_literal_fallback(tmp_path: Path) -> None:
    roots = (tmp_path / "missing",)

    assert _resolve_metric(roots, {"primary_metric": " balanced_accuracy "}) == (
        "balanced_accuracy"
    )
    assert _resolve_metric(roots, {"primary_metric": " "}) == "primary"
    assert _resolve_metric(roots, None) == "primary"


def test_resolver_does_not_read_legacy_athena_evaluator_spec(tmp_path: Path) -> None:
    legacy = tmp_path / ".athena" / "evaluator_spec.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"primary_metric": "accuracy"}), encoding="utf-8")

    assert _resolve_metric((tmp_path / "missing",), None) == "primary"

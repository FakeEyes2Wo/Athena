"""Evaluator behavioral property tests."""

import asyncio
import csv
import io
from types import SimpleNamespace

import pytest

from athena.research.evaluation.trust import (
    _parse_labels,
    extract_prediction_column,
    extract_prediction_column_from_source,
    validate_evaluator_properties,
)

_LABELS = "__athena_row_id,label\n0,0\n1,1\n2,0\n3,1\n4,0\n5,1\n"


class _RowOrderBroken:
    """Score depends on row order, so row-order invariance must fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, predictions_csv: str) -> float:
        self.calls.append(predictions_csv)
        # Intentionally order-dependent fake: first row id.
        lines = predictions_csv.strip().splitlines()[1:]
        return float(lines[0].split(",")[0]) if lines else 0.0


class _ValueInsensitive:
    """Score ignores values, so permutation sensitivity must fail."""

    async def __call__(self, predictions_csv: str) -> float:
        return 0.5


class _GoodEvaluator:
    """Score = fraction of correct predictions, independent of row order."""

    async def __call__(self, predictions_csv: str) -> float:
        preds = dict(
            line.split(",") for line in predictions_csv.strip().splitlines()[1:]
        )
        labels = dict(line.split(",") for line in _LABELS.strip().splitlines()[1:])
        correct = sum(
            1 for row_id, label in labels.items() if preds.get(row_id) == label
        )
        return correct / len(labels)


class _PredColumnEvaluator:
    """A tabular evaluator that expects the prediction column to be named ``pred``."""

    async def __call__(self, predictions_csv: str) -> float:
        reader = csv.DictReader(io.StringIO(predictions_csv))
        if "pred" not in (reader.fieldnames or []):
            return 0.0
        preds = {row["__athena_row_id"]: row["pred"] for row in reader}
        labels = dict(line.split(",") for line in _LABELS.strip().splitlines()[1:])
        correct = sum(
            1 for row_id, label in labels.items() if preds.get(row_id) == label
        )
        return correct / len(labels)


def test_parse_labels_extracts_ids_and_targets() -> None:
    ids, values = _parse_labels(_LABELS)
    assert ids == ["0", "1", "2", "3", "4", "5"]
    assert values == ["0", "1", "0", "1", "0", "1"]


def test_extract_prediction_column_returns_none_when_not_declared() -> None:
    assert extract_prediction_column("") is None
    assert extract_prediction_column("# arbitrary handoff\n") is None


def test_extract_prediction_column_parses_explicit_declaration() -> None:
    handoff = (
        "# Evaluator Handoff\n" "prediction column: pred\n" "join on __athena_row_id\n"
    )
    assert extract_prediction_column(handoff) == "pred"


def test_extract_prediction_column_accepts_alternative_phrasing() -> None:
    handoff = (
        "The predictions file uses the exact schema `__athena_row_id,pred`.\n"
        "prediction_column = pred\n"
    )
    assert extract_prediction_column(handoff) == "pred"


def test_extract_prediction_column_from_source_reads_row_pred() -> None:
    source = (
        "import csv\n"
        "with open('predictions/predictions.csv') as f:\n"
        "    for row in csv.DictReader(f):\n"
        "        value = row['pred']\n"
    )
    assert extract_prediction_column_from_source(source) == "pred"


def test_extract_prediction_column_from_source_reads_dataframe_column() -> None:
    source = (
        "import pandas as pd\n"
        "preds = pd.read_csv('predictions/predictions.csv')\n"
        "score = preds['probability']\n"
    )
    assert extract_prediction_column_from_source(source) == "probability"


def test_extract_prediction_column_from_source_ignores_identity_and_label() -> None:
    source = (
        "for row in reader:\n"
        "    ident = row['__athena_row_id']\n"
        "    truth = row['label']\n"
    )
    assert extract_prediction_column_from_source(source) is None


@pytest.mark.asyncio
async def test_row_order_invariance_failure_is_detected() -> None:
    outcome = await validate_evaluator_properties(
        _LABELS, _RowOrderBroken(), prediction_column="prediction"
    )
    assert outcome["ok"] is False
    assert "row-order" in outcome["reason"]


@pytest.mark.asyncio
async def test_value_permutation_sensitivity_failure_is_detected() -> None:
    outcome = await validate_evaluator_properties(
        _LABELS, _ValueInsensitive(), prediction_column="prediction"
    )
    assert outcome["ok"] is False
    assert "permutation" in outcome["reason"]


@pytest.mark.asyncio
async def test_good_evaluator_passes_property_tests() -> None:
    outcome = await validate_evaluator_properties(
        _LABELS, _GoodEvaluator(), prediction_column="prediction"
    )
    assert outcome["ok"] is True


@pytest.mark.asyncio
async def test_pred_column_evaluator_passes_with_declared_prediction_column() -> None:
    outcome = await validate_evaluator_properties(
        _LABELS, _PredColumnEvaluator(), prediction_column="pred"
    )
    assert outcome["ok"] is True


@pytest.mark.asyncio
async def test_pred_column_evaluator_fails_with_wrong_column() -> None:
    # If the probe still writes the historical "prediction" header, an evaluator
    # that only reads "pred" will be treated as value-insensitive.
    outcome = await validate_evaluator_properties(
        _LABELS, _PredColumnEvaluator(), prediction_column="prediction"
    )
    assert outcome["ok"] is False
    assert "permutation" in outcome["reason"]


@pytest.mark.asyncio
async def test_validate_rejects_too_few_rows() -> None:
    outcome = await validate_evaluator_properties(
        "__athena_row_id,label\n0,0\n",
        _GoodEvaluator(),
        prediction_column="prediction",
    )
    assert outcome["ok"] is False


@pytest.mark.asyncio
async def test_property_probe_uses_declared_dataset_columns() -> None:
    labels = "sample_key,target\na,0\nb,1\nc,0\nd,1\n"
    seen: list[str] = []

    async def score(predictions_csv: str) -> float:
        seen.append(predictions_csv)
        rows = list(csv.DictReader(io.StringIO(predictions_csv)))
        truth = {"a": "0", "b": "1", "c": "0", "d": "1"}
        return sum(truth[row["sample_key"]] == row["pred_label"] for row in rows)

    outcome = await validate_evaluator_properties(
        labels,
        score,
        spec=SimpleNamespace(
            prediction_id_column="sample_key",
            prediction_column="pred_label",
            probability_columns=("prob_0", "prob_1"),
        ),
    )

    assert outcome["ok"] is True
    assert seen[0].splitlines()[0] == "sample_key,pred_label,prob_0,prob_1"

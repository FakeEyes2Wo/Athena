"""Evaluator behavioral property tests."""

import asyncio

import pytest

from athena.research.evaluator_trust import (
    _parse_labels,
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
        labels = dict(
            line.split(",") for line in _LABELS.strip().splitlines()[1:]
        )
        correct = sum(1 for row_id, label in labels.items() if preds.get(row_id) == label)
        return correct / len(labels)


def test_parse_labels_extracts_ids_and_targets() -> None:
    ids, values = _parse_labels(_LABELS)
    assert ids == ["0", "1", "2", "3", "4", "5"]
    assert values == ["0", "1", "0", "1", "0", "1"]


@pytest.mark.asyncio
async def test_row_order_invariance_failure_is_detected() -> None:
    outcome = await validate_evaluator_properties(_LABELS, _RowOrderBroken())
    assert outcome["ok"] is False
    assert "row-order" in outcome["reason"]


@pytest.mark.asyncio
async def test_value_permutation_sensitivity_failure_is_detected() -> None:
    outcome = await validate_evaluator_properties(_LABELS, _ValueInsensitive())
    assert outcome["ok"] is False
    assert "permutation" in outcome["reason"]


@pytest.mark.asyncio
async def test_good_evaluator_passes_property_tests() -> None:
    outcome = await validate_evaluator_properties(_LABELS, _GoodEvaluator())
    assert outcome["ok"] is True


@pytest.mark.asyncio
async def test_validate_rejects_too_few_rows() -> None:
    outcome = await validate_evaluator_properties(
        "__athena_row_id,label\n0,0\n",
        _GoodEvaluator(),
    )
    assert outcome["ok"] is False

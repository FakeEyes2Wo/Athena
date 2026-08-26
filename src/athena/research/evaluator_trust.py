"""Deterministic behavioral property tests for trusted evaluators.

The platform must verify an LLM-written evaluator before freezing it as a
trusted authority. These tests do not depend on the metric's meaning; they
check the two properties that make a row-id join trustworthy:

1. Row-order invariance: shuffling prediction rows must not change the score.
2. Value-permutation sensitivity: permuting prediction values among ids must
   change the score on non-degenerate data.
"""

import csv
import io
import random
from collections.abc import Awaitable, Callable
from typing import Any

ScoreFn = Callable[[str], Awaitable[float]]


def _build_predictions(
    ids: list[str], values: list[str]
) -> str:
    lines = ["__athena_row_id,prediction"]
    lines.extend(f"{row_id},{value}" for row_id, value in zip(ids, values))
    return "\n".join(lines) + "\n"


def _parse_labels(labels_csv: str) -> tuple[list[str], list[str]]:
    reader = csv.DictReader(io.StringIO(labels_csv))
    if reader.fieldnames is None or "__athena_row_id" not in reader.fieldnames:
        raise ValueError("labels CSV must contain __athena_row_id")
    target_fields = [
        field for field in reader.fieldnames if field != "__athena_row_id"
    ]
    if not target_fields:
        raise ValueError("labels CSV must contain a target column")
    target_field = target_fields[0]
    rows = list(reader)
    ids = [row["__athena_row_id"].strip() for row in rows]
    values = [row[target_field] for row in rows]
    return ids, values


async def validate_evaluator_properties(
    labels_csv: str,
    score: ScoreFn,
    *,
    seed: int = 0,
) -> dict[str, Any]:
    """Run row-order and value-permutation checks against a scoring callable.

    Returns ``{"ok": True}`` or ``{"ok": False, "reason": ...}``. Raises
    ``ValueError`` only for malformed label input.
    """
    ids, values = _parse_labels(labels_csv)
    if len(ids) < 2:
        return {"ok": False, "reason": "need at least two labeled rows"}

    rng = random.Random(seed)
    baseline = await score(_build_predictions(ids, values))

    # Row-order invariance: shuffle complete (id, value) pairs, not values.
    pairs = list(zip(ids, values))
    rng.shuffle(pairs)
    shuffled_ids, shuffled_values = zip(*pairs) if pairs else ((), ())
    shuffled = await score(_build_predictions(list(shuffled_ids), list(shuffled_values)))
    if shuffled != baseline:
        return {
            "ok": False,
            "reason": "row-order invariance failed: shuffling rows changed the score",
        }

    # Value-permutation sensitivity: deterministically swap two rows with
    # different target values so a correct-prediction metric must change.
    different = [
        (i, j)
        for i in range(len(values))
        for j in range(i + 1, len(values))
        if values[i] != values[j]
    ]
    if not different:
        return {"ok": True, "note": "all labels identical; permutation not applicable"}
    i, j = different[0]
    permuted = values[:]
    permuted[i], permuted[j] = permuted[j], permuted[i]
    permuted_score = await score(_build_predictions(ids, permuted))
    if permuted_score == baseline:
        return {
            "ok": False,
            "reason": "value-permutation sensitivity failed: permuting values "
            "did not change the score",
        }

    return {"ok": True}

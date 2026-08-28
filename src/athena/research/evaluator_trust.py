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
import re
from collections.abc import Awaitable, Callable
from typing import Any

ScoreFn = Callable[[str], Awaitable[float]]

# HANDOFF.md 是自由文本，但 evaluator prompt 要求它“精确声明 schema”。
# 这里只做保守解析：优先匹配“prediction column: xxx / pred column: xxx /
# prediction field: xxx”这类显式声明；解析不到时返回 None，由调用方拒绝。
_PREDICTION_COLUMN_PATTERNS = (
    re.compile(
        r"prediction[_ ]*column\s*[:=]\s*[`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`\"]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"prediction[_ ]*field\s*[:=]\s*[`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`\"]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"predictions?[_ ]*column\s*[:=]\s*[`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`\"]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bpred\b\s*(?:column|field)?\s*[:=]\s*[`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`\"]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"schema[^,\n]*,\s*[`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`\"]?",
        re.IGNORECASE,
    ),
)


def extract_prediction_column(handoff_text: str) -> str | None:
    """Parse the prediction CSV column name from evaluator HANDOFF.md.

    Returns the declared column name, or ``None`` when HANDOFF.md does not
    contain an explicit, parseable declaration. There is no compatibility
    fallback: a tabular evaluator must declare the column it reads.
    """
    if not handoff_text:
        return None
    for pattern in _PREDICTION_COLUMN_PATTERNS:
        match = pattern.search(handoff_text)
        if match:
            name = match.group("name")
            # 不可能用行 id 自己当预测值列；出现这种情况说明匹配到了 schema 的行 id。
            if name and name.lower() != "__athena_row_id":
                return name
    return None


_SOURCE_COLUMN_RE = re.compile(
    r"\b(?:row|df|data|preds|predictions|reader|frame)\s*\[\s*[`'\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`'\"]?\s*\]",
    re.IGNORECASE,
)

# 这些是 labels / row-id / 索引等不可能作为“预测值列”的常见名称。
_SOURCE_COLUMN_STOPWORDS = {
    "__athena_row_id",
    "row_id",
    "id",
    "index",
    "label",
    "labels",
    "target",
    "truth",
    "y",
    "answer",
    "ground_truth",
}


def extract_prediction_column_from_source(evaluator_source: str) -> str | None:
    """Best-effort extract the prediction CSV column from ``evaluate.py`` source.

    This does not depend on a prompt-specified HANDOFF.md wording. It scans the
    evaluator for the most common tabular CSV access patterns:
    ``row["col"]``, ``df["col"]``, ``reader["col"]``, etc., and returns the
    first non-identity/non-label column that looks like the prediction value.
    """
    if not evaluator_source:
        return None
    candidates: list[str] = []
    for match in _SOURCE_COLUMN_RE.finditer(evaluator_source):
        name = match.group("name")
        if name.lower() in _SOURCE_COLUMN_STOPWORDS:
            continue
        if name not in candidates:
            candidates.append(name)
    if not candidates:
        return None
    # Prefer the most explicit prediction-ish names; otherwise keep first.
    for preferred in ("prediction", "pred", "prob", "probability", "score", "proba"):
        if preferred in candidates:
            return preferred
    return candidates[0]


def _build_predictions(
    ids: list[str], values: list[str], *, prediction_column: str
) -> str:
    lines = [f"__athena_row_id,{prediction_column}"]
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
    prediction_column: str,
) -> dict[str, Any]:
    """Run row-order and value-permutation checks against a scoring callable.

    ``prediction_column`` is the CSV prediction column name that the evaluator
    actually expects. Callers must parse it from HANDOFF.md via
    :func:`extract_prediction_column`; there is no default compatibility column.

    Returns ``{"ok": True}`` or ``{"ok": False, "reason": ...}``. Raises
    ``ValueError`` only for malformed label input.
    """
    ids, values = _parse_labels(labels_csv)
    if len(ids) < 2:
        return {"ok": False, "reason": "need at least two labeled rows"}

    rng = random.Random(seed)
    baseline = await score(_build_predictions(ids, values, prediction_column=prediction_column))

    # Row-order invariance: shuffle complete (id, value) pairs, not values.
    pairs = list(zip(ids, values))
    rng.shuffle(pairs)
    shuffled_ids, shuffled_values = zip(*pairs) if pairs else ((), ())
    shuffled = await score(
        _build_predictions(
            list(shuffled_ids), list(shuffled_values), prediction_column=prediction_column
        )
    )
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
    permuted_score = await score(
        _build_predictions(ids, permuted, prediction_column=prediction_column)
    )
    if permuted_score == baseline:
        return {
            "ok": False,
            "reason": "value-permutation sensitivity failed: permuting values "
            "did not change the score",
        }

    return {"ok": True}

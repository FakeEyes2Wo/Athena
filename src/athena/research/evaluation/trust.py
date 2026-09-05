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
from collections.abc import Awaitable, Callable, Sequence

ScoreFn = Callable[[str], Awaitable[float]]

# HANDOFF.md 是自由文本，但 evaluator prompt 要求它“精确声明 schema”。
# 这里只做保守解析：优先匹配“prediction column: xxx / pred column: xxx /
# prediction field: xxx”这类显式声明；解析不到时返回 None，由调用方拒绝。
_PREDICTION_COLUMN_PATTERNS = (
    re.compile(
        r"predictions?[_ ]*(?:column|field)\s*[:=]\s*"
        r"[`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`\"]?",
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
            if name and not _looks_like_identity(name):
                return name
    return None


_SOURCE_COLUMN_RE = re.compile(
    r"\b(?:row|df|data|preds|predictions|reader|frame)\s*\[\s*[`'\"]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`'\"]?\s*\]",
    re.IGNORECASE,
)

# 这些是 labels / row-id / 索引等不可能作为“预测值列”的常见名称。
_SOURCE_COLUMN_STOPWORDS = {
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


def _looks_like_identity(name: str) -> bool:
    """Return whether a column name describes row identity rather than a value."""
    lowered = name.lower()
    return lowered in _SOURCE_COLUMN_STOPWORDS or lowered.endswith("_id")


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
        if _looks_like_identity(name):
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
    ids: list[str],
    values: list[str],
    *,
    prediction_column: str,
    prediction_id_column: str,
    probability_columns: Sequence[str] = (),
) -> str:
    columns = [prediction_id_column, prediction_column, *probability_columns]
    lines = [",".join(columns)]
    rows = []
    for row_id, value in zip(ids, values):
        probabilities = [_probability(value, column) for column in probability_columns]
        rows.append(",".join([row_id, value, *probabilities]))
    lines.extend(rows)
    return "\n".join(lines) + "\n"


def _probability(value: str, column: str) -> str:
    """Create a deterministic probe probability for a declared class column."""
    suffix = column.lower().removeprefix("prob_").removeprefix("probability_")
    return "1.0" if suffix and suffix == value.strip().lower() else "0.0"


def _parse_labels(
    labels_csv: str, *, id_column: str | None = None
) -> tuple[list[str], list[str], str]:
    reader = csv.DictReader(io.StringIO(labels_csv))
    if not reader.fieldnames:
        raise ValueError("labels CSV must contain an identity column")
    id_column = id_column or reader.fieldnames[0]
    if id_column not in reader.fieldnames:
        raise ValueError(f"labels CSV must contain {id_column!r}")
    target_fields = [field for field in reader.fieldnames if field != id_column]
    if not target_fields:
        raise ValueError("labels CSV must contain a target column")
    target_field = target_fields[0]
    rows = list(reader)
    ids = [row[id_column].strip() for row in rows]
    values = [row[target_field] for row in rows]
    return ids, values, id_column


async def validate_evaluator_properties(
    labels_csv: str,
    score: ScoreFn,
    *,
    seed: int = 0,
    prediction_column: str | None = None,
    prediction_id_column: str | None = None,
    probability_columns: Sequence[str] = (),
) -> dict[str, bool | str]:
    """Run row-order and value-permutation checks against a scoring callable.

    Returns ``{"ok": True}`` or ``{"ok": False, "reason": ...}``. Raises
    ``ValueError`` only for malformed label input.
    """
    if not prediction_column:
        raise ValueError("evaluator must declare a prediction column")
    ids, values, prediction_id_column = _parse_labels(
        labels_csv, id_column=prediction_id_column
    )
    if len(ids) < 2:
        return {"ok": False, "reason": "need at least two labeled rows"}

    rng = random.Random(seed)
    baseline = await score(
        _build_predictions(
            ids,
            values,
            prediction_column=prediction_column,
            prediction_id_column=prediction_id_column,
            probability_columns=probability_columns,
        )
    )

    # Row-order invariance: shuffle complete (id, value) pairs, not values.
    pairs = list(zip(ids, values))
    rng.shuffle(pairs)
    shuffled_ids, shuffled_values = map(list, zip(*pairs))
    shuffled = await score(
        _build_predictions(
            shuffled_ids,
            shuffled_values,
            prediction_column=prediction_column,
            prediction_id_column=prediction_id_column,
            probability_columns=probability_columns,
        )
    )
    if shuffled != baseline:
        return {
            "ok": False,
            "reason": "row-order invariance failed: shuffling rows changed the score",
        }

    # Value-permutation sensitivity: deterministically swap two rows with
    # different target values so a correct-prediction metric must change.
    different_index = next(
        (
            index
            for index, value in enumerate(values[1:], start=1)
            if value != values[0]
        ),
        None,
    )
    if different_index is None:
        return {"ok": True, "note": "all labels identical; permutation not applicable"}
    permuted = values[:]
    permuted[0], permuted[different_index] = (
        permuted[different_index],
        permuted[0],
    )
    permuted_score = await score(
        _build_predictions(
            ids,
            permuted,
            prediction_column=prediction_column,
            prediction_id_column=prediction_id_column,
            probability_columns=probability_columns,
        )
    )
    if permuted_score == baseline:
        return {
            "ok": False,
            "reason": "value-permutation sensitivity failed: permuting values "
            "did not change the score",
        }

    return {"ok": True}

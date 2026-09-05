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
    """Create a deterministic probe probability for a declared class column.

    ``prob_1``/``probability_0`` name their class, so the probe emits the
    one-hot indicator. A column naming no class (``pred_proba``, ``score``)
    means P(positive), so it gets the label itself: emitting "0.0" for every
    row hands the evaluator a constant column, and an evaluator that prefers
    that column can then never show permutation sensitivity. The 2026-09-03
    TESS run spent its entire evaluator turn budget being rejected for exactly
    that, and PREPARE failed.
    """
    normalised = value.strip().lower()
    lowered = column.lower()
    for prefix in ("prob_", "probability_"):
        if lowered.startswith(prefix):
            return "1.0" if lowered.removeprefix(prefix) == normalised else "0.0"
    try:
        float(normalised)
    except ValueError:  # 非数值标签配通用概率列：写回去会让评估器的 float() 崩
        return "0.0"
    return normalised


def _parse_labels(
    labels_csv: str, *, id_column: str | None = None
) -> tuple[list[str], list[str]]:
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
    return ids, values


async def validate_evaluator_properties(
    labels_csv: str,
    score: ScoreFn,
    *,
    seed: int = 0,
    prediction_column: str | None = None,
    prediction_id_column: str | None = None,
    probability_columns: Sequence[str] = (),
    spec: Any | None = None,
) -> dict[str, Any]:
    """Run row-order and value-permutation checks against a scoring callable.

    The declared evaluator spec may provide the identity, prediction, and
    probability columns.  The explicit keyword arguments remain available to
    old callers and are ignored when ``spec`` supplies the same declarations.

    Returns ``{"ok": True}`` or ``{"ok": False, "reason": ...}``. Raises
    ``ValueError`` only for malformed label input.
    """
    if spec is not None:
        prediction_column = getattr(spec, "prediction_column", prediction_column)
        prediction_id_column = getattr(
            spec, "prediction_id_column", prediction_id_column
        )
        probability_columns = tuple(
            getattr(spec, "probability_columns", probability_columns) or ()
        )
    if not prediction_column:
        raise ValueError("evaluator must declare a prediction column")
    ids, values = _parse_labels(labels_csv, id_column=prediction_id_column)
    prediction_id_column = prediction_id_column or _first_csv_column(labels_csv)
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
    shuffled_ids, shuffled_values = zip(*pairs) if pairs else ((), ())
    shuffled = await score(
        _build_predictions(
            list(shuffled_ids),
            list(shuffled_values),
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


def _first_csv_column(labels_csv: str) -> str:
    """Return the first declared labels column for legacy probe callers."""
    reader = csv.reader(io.StringIO(labels_csv))
    try:
        return next(reader)[0]
    except (StopIteration, IndexError) as exc:
        raise ValueError("labels CSV must declare an identity column") from exc

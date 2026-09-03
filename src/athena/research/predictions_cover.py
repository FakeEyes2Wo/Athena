"""Assert a predictions file answers the rows it will be scored against.

A trusted evaluator joins predictions to labels on ``__athena_row_id`` and
reports whatever the join produced. That number is meaningful only if the
predictions actually cover the rows being scored, and nothing about a wrong
answer looks wrong: the file parses, the ids are integers, the row count can
even match exactly, and the score comes back as an ordinary float.

Two real incidents, both the same shape from opposite ends:

- 2026-08-30, VALIDATE: a candidate hardcoded the SEARCH feature path, so
  re-running its frozen argv produced predictions for rows VALIDATE was not
  scoring. The symptom was ``{"primary": 0.0}``, which reads like a broken
  evaluator or a broken model and is neither.

- 2026-09-02, PREPARE: a baseline wrote positional indices as row ids --
  ``0, 1, 2, ...`` against labels spanning ``304 … 846890``. 36,674 of 169,725
  label rows found a match, every one of them pairing a label with some other
  window's prediction. The agent measured PR-AUC 0.7982 for itself; the trusted
  evaluator returned **0.016331**, and the framework accepted that as the
  reference metric for the whole run. The evaluator did warn -- to stderr, where
  nobody read it -- and reported the shrunken ``test_n`` inside a JSON blob that
  is only ever read for ``primary``.

The second one is the more expensive: a bad *reference* metric does not fail a
run, it silently re-scales it. Every later candidate is compared against 0.0163,
so the first one that merely writes correct ids looks like a breakthrough.
"""

import csv
from pathlib import Path

ROW_ID_COLUMN = "__athena_row_id"


class PredictionsCoverageError(ValueError):
    """Predictions do not answer the rows they are about to be scored on."""


def _read_row_ids(path: Path) -> tuple[bool, set[str]]:
    """Return whether a CSV declares row ids and its validated values."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or ROW_ID_COLUMN not in reader.fieldnames:
            return False, set()
        values: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            raw = row.get(ROW_ID_COLUMN)
            if raw is None or not raw.strip():
                raise PredictionsCoverageError(
                    f"{path.name} row {row_number} is missing {ROW_ID_COLUMN}"
                )
            values.add(raw.strip())
        return True, values


def row_ids(path: Path) -> set[str]:
    """Row ids in one CSV, or an empty set if it carries no id column."""
    return _read_row_ids(path)[1]


def assert_predictions_cover(predictions_dir: Path, expected_csv: Path) -> None:
    """Fail with the real cause when predictions answer the wrong rows.

    Silent when the expected data has no row-id column: not every task is
    tabular. When expected ids exist, predictions must provide usable ids so
    coverage cannot degrade into an unchecked evaluator join.
    """
    expected_has_ids, expected = _read_row_ids(expected_csv)
    if not expected_has_ids or not expected:
        return
    produced: set[str] = set()
    produced_has_ids = False
    for path in sorted(predictions_dir.rglob("*.csv")):
        has_ids, values = _read_row_ids(path)
        produced_has_ids |= has_ids
        produced |= values
    if not produced_has_ids or not produced:
        raise PredictionsCoverageError(
            f"prediction CSV files contain no usable {ROW_ID_COLUMN}; copy that "
            "column from the file named by ATHENA_PREDICT_FEATURES"
        )
    missing = expected - produced
    if not missing:
        return
    overlap = len(expected & produced)
    detail = (
        f"{len(missing)} of {len(expected)} rows in {expected_csv.name} have no "
        f"prediction (overlap {overlap})."
    )
    if overlap == 0:
        detail += (
            " Zero overlap means the predictions answer a different split "
            "entirely: read the feature file named by ATHENA_PREDICT_FEATURES "
            "and carry its __athena_row_id values through to your output."
        )
    else:
        # 部分重叠几乎总是「把行号当成了行 id」：文件能解析、id 是整数、
        # 行数甚至可能刚好对上，只有 join 的结果是错的。
        detail += (
            " A partial overlap usually means positional indices were written "
            "as row ids. Copy the __athena_row_id column from the feature file "
            "named by ATHENA_PREDICT_FEATURES instead of numbering the rows."
        )
    raise PredictionsCoverageError(detail)


__all__ = [
    "ROW_ID_COLUMN",
    "PredictionsCoverageError",
    "assert_predictions_cover",
    "row_ids",
]

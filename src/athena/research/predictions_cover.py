"""Assert a predictions file answers the rows it will be scored against.

A trusted evaluator joins predictions to labels on the split's identity column
and reports whatever the join produced. That number is meaningful only if the
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

VALIDATE has carried this check since the first incident. It lives here so
PREPARE and SEARCH — where the number that anchors the whole run is produced —
can run it too.
"""

import csv
from collections import Counter
from pathlib import Path


class PredictionsCoverageError(ValueError):
    """Predictions do not answer the rows they are about to be scored on.

    A ``ValueError`` subclass: callers that already treated a coverage failure
    as a bad-output error keep working unchanged.
    """


def row_ids(path: Path) -> tuple[list[str], set[str]]:
    """Read the identity values from the first column of a CSV.

    The split writer currently places its canonical identity first, but its
    name is deliberately not part of validation.  This keeps the check usable
    for datasets whose public prediction contract calls that column
    ``image_filename``, ``sample_id``, or another dataset-specific name.

    Returns the values in file order and the subset that occurs more than once.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return [], set()
        identity = reader.fieldnames[0]
        values = [row.get(identity, "").strip() for row in reader]
    values = [value for value in values if value]
    duplicates = {value for value, count in Counter(values).items() if count > 1}
    return values, duplicates


def assert_predictions_cover(predictions_dir: Path, expected_csv: Path) -> None:
    """Fail with the real cause when predictions answer the wrong rows.

    Silent when the expected data carries no identity values: not every task is
    tabular.  When they exist, the predictions must cover them, must not repeat
    one, and must not invent rows the split does not contain.
    """
    expected_values, expected_duplicates = row_ids(expected_csv)
    expected = set(expected_values)
    if not expected:
        return
    if expected_duplicates:
        raise PredictionsCoverageError(
            f"expected feature file {expected_csv.name} has duplicate identity "
            f"values: {sorted(expected_duplicates)[:5]}"
        )
    produced_counts: Counter[str] = Counter()
    for path in sorted(predictions_dir.rglob("*.csv")):
        values, _duplicates = row_ids(path)
        produced_counts.update(values)
    produced = set(produced_counts)
    produced_duplicates = {
        value for value, count in produced_counts.items() if count > 1
    }
    if produced_duplicates:
        raise PredictionsCoverageError(
            "predictions contain duplicate identity values: "
            f"{sorted(produced_duplicates)[:5]}"
        )
    if not produced:
        return
    extra = produced - expected
    missing = expected - produced
    if not missing:
        if extra:
            raise PredictionsCoverageError(
                f"predictions contain {len(extra)} identity values absent from "
                f"{expected_csv.name}: {sorted(extra)[:5]}"
            )
        return
    overlap = len(expected & produced)
    detail = (
        f"{len(missing)} of {len(expected)} rows in {expected_csv.name} have no "
        f"prediction (overlap {overlap})."
    )
    if extra:
        detail += f" Also found {len(extra)} unexpected identity values."
    if overlap == 0:
        detail += (
            " Zero overlap means the predictions answer a different split "
            "entirely: the command hardcoded its feature path instead of "
            "reading ATHENA_PREDICT_FEATURES, so it cannot be pointed at the "
            "rows being scored."
        )
    else:
        # 部分重叠几乎总是「把行号当成了行 id」：文件能解析、id 是整数、
        # 行数甚至可能刚好对上，只有 join 的结果是错的。
        detail += (
            " A partial overlap usually means positional indices were written "
            "as row ids. Carry the identity column from the feature file named "
            "by ATHENA_PREDICT_FEATURES through to your output instead of "
            "numbering the rows."
        )
    raise PredictionsCoverageError(detail)


__all__ = [
    "PredictionsCoverageError",
    "assert_predictions_cover",
    "row_ids",
]

"""Prediction row coverage checks shared by SEARCH and VALIDATE."""

import csv
from collections import Counter
from pathlib import Path


class PredictionInputError(ValueError):
    """The frozen feature input is unusable; candidate code cannot repair it."""


def _csv_ids(path: Path, identity_column: str | None) -> list[str]:
    """Read CSV identities, using the first column only for legacy contracts."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            raise ValueError(f"CSV {path.name} is empty or has no header")
        header = [name.strip() for name in header]
        if any(not name for name in header):
            raise ValueError(f"CSV {path.name} has an empty column name")
        if len(set(header)) != len(header):
            raise ValueError(f"CSV {path.name} has duplicate column names")
        column = identity_column or header[0]
        if column not in header:
            raise ValueError(
                f"CSV {path.name} is missing declared identity column {column!r}"
            )
        index = header.index(column)
        values: list[str] = []
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise ValueError(
                    f"CSV {path.name} row {row_number} has {len(row)} values; "
                    f"expected {len(header)}"
                )
            value = row[index].strip()
            if not value:
                raise ValueError(
                    f"CSV {path.name} row {row_number} has an empty identity value"
                )
            values.append(value)
    return values


def assert_predictions_cover(
    predictions_dir: Path,
    expected_csv: Path,
    *,
    prediction_file: str | None = None,
    prediction_id_column: str | None = None,
    expected_id_column: str | None = None,
    prediction_format: str = "tabular_csv",
) -> None:
    """Require exactly one prediction for every expected tabular CSV row.

    Non-CSV and explicitly non-tabular formats are skipped because row identity
    cannot be checked with this contract. With no declared identity field, the
    first-column convention remains for compatibility with legacy evaluators.
    """
    if prediction_format != "tabular_csv" or expected_csv.suffix.lower() != ".csv":
        return
    try:
        if expected_id_column is None and prediction_id_column is not None:
            with expected_csv.open(encoding="utf-8-sig", newline="") as handle:
                header = [name.strip() for name in next(csv.reader(handle), [])]
            if prediction_id_column in header:
                expected_id_column = prediction_id_column
        expected_values = _csv_ids(expected_csv, expected_id_column)
    except (OSError, ValueError) as exc:
        # Trigger: frozen feature CSV cannot be read or its declared prediction-ID column is invalid.
        raise PredictionInputError(f"invalid frozen feature input: {exc}") from exc
    if not expected_values:
        raise PredictionInputError(
            f"expected feature file {expected_csv.name} contains no rows"
        )
    expected_counts = Counter(expected_values)
    expected_duplicates = [key for key, count in expected_counts.items() if count > 1]
    if expected_duplicates:
        raise PredictionInputError(
            f"expected feature file {expected_csv.name} has duplicate identity "
            f"values: {sorted(expected_duplicates)[:5]}"
        )

    if prediction_file is not None:
        candidate = Path(prediction_file)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("prediction_file must stay within predictions directory")
        files = [predictions_dir / candidate]
        if not files[0].is_file():
            raise ValueError(f"declared prediction file {prediction_file!r} is missing")
    else:
        files = sorted(predictions_dir.rglob("*.csv"))
    produced_values: list[str] = []
    for path in files:
        if path.suffix.lower() == ".csv":
            produced_values.extend(_csv_ids(path, prediction_id_column))
    if not produced_values:
        raise ValueError("predictions contain no identity rows")

    produced_counts = Counter(produced_values)
    produced_duplicates = [key for key, count in produced_counts.items() if count > 1]
    if produced_duplicates:
        raise ValueError(
            "predictions contain duplicate identity values: "
            f"{sorted(produced_duplicates)[:5]}"
        )
    expected, produced = set(expected_values), set(produced_values)
    missing, extra = expected - produced, produced - expected
    if missing or extra:
        detail = []
        if missing:
            detail.append(
                f"{len(missing)} of {len(expected)} rows have no prediction "
                f"(overlap {len(expected & produced)})"
            )
        if extra:
            detail.append(
                f"{len(extra)} identity values are absent from {expected_csv.name}"
            )
        if missing and len(missing) == len(expected):
            detail.append(
                "zero overlap means the run predicted a different split; read "
                "ATHENA_PREDICT_FEATURES instead of a hardcoded feature path"
            )
        raise ValueError(
            f"prediction coverage mismatch for {expected_csv.name}: "
            + "; ".join(detail)
            + f" (missing={sorted(missing)[:5]}, extra={sorted(extra)[:5]})"
        )

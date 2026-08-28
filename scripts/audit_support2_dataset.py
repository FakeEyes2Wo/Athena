"""Verify that the prepared SUPPORT2 task uses the complete public dataset.

The audit is deliberately read-only.  It reconstructs the 180-day target from
the source CSV, compares every prepared row and feature, and checks immutable
file hashes recorded for the public download used by the live regression run.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

FEATURE_COLUMNS = (
    "age",
    "dzgroup",
    "hday",
    "ca",
    "scoma",
    "meanbp",
    "wblc",
    "hrt",
    "resp",
    "temp",
    "pafi",
    "alb",
    "bili",
    "crea",
    "sod",
    "ph",
)
TARGET_COLUMN = "mortality_180d"
OFFICIAL_SOURCE_URL = "https://hbiostat.org/data/repo/support2csv.zip"
OFFICIAL_ARCHIVE_SHA256 = (
    "8ed43980742a18e1847a8dfc5530bc4b30564ad9e4ad1b1b50bbc5d29d8c86fe"
)
OFFICIAL_RAW_ROWS = 9_105
OFFICIAL_TARGET_COUNTS = {0: 4_840, 1: 4_265}
OFFICIAL_RAW_SHA256 = "79621945edf2a5c8dc36359684ff356d3c6025e773ba4fefac26f865f7894c78"
OFFICIAL_PREPARED_SHA256 = (
    "df8f44eaa79cad65a4cf562b08adf4609292dab4caacfcd672f8e4bf9cb4b563"
)


def file_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 digest for one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_prepared(raw: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Recreate the documented 180-day cohort and binary target."""
    required = {*FEATURE_COLUMNS, "death", "d.time"}
    missing = sorted(required.difference(raw.columns))
    if missing:
        raise ValueError(f"raw SUPPORT2 columns missing: {missing}")

    death = pd.to_numeric(raw["death"], errors="coerce")
    days = pd.to_numeric(raw["d.time"], errors="coerce")
    known_180 = (death.eq(1) & days.le(180)) | days.ge(180)
    target = (death.eq(1) & days.le(180)).astype("int8")

    expected = raw.loc[known_180, list(FEATURE_COLUMNS)].reset_index(drop=True)
    expected[TARGET_COLUMN] = target.loc[known_180].reset_index(drop=True)
    return expected, int((~known_180).sum())


def audit_support2(
    raw_path: Path,
    prepared_path: Path,
    *,
    archive_path: Path | None = None,
    expected_archive_sha256: str | None = OFFICIAL_ARCHIVE_SHA256,
    expected_raw_rows: int = OFFICIAL_RAW_ROWS,
    expected_raw_sha256: str | None = OFFICIAL_RAW_SHA256,
    expected_prepared_sha256: str | None = OFFICIAL_PREPARED_SHA256,
    expected_target_counts: dict[int, int] | None = OFFICIAL_TARGET_COUNTS,
) -> dict[str, Any]:
    """Audit provenance, cohort membership, row order, features, and target."""
    raw_path = raw_path.resolve()
    prepared_path = prepared_path.resolve()
    archive_path = archive_path.resolve() if archive_path is not None else None
    raw_hash = file_sha256(raw_path)
    prepared_hash = file_sha256(prepared_path)
    archive_hash = file_sha256(archive_path) if archive_path is not None else None

    if (
        archive_hash is not None
        and expected_archive_sha256
        and archive_hash != expected_archive_sha256.lower()
    ):
        raise ValueError(
            "source archive SHA-256 mismatch: expected "
            f"{expected_archive_sha256}, got {archive_hash}"
        )

    if expected_raw_sha256 and raw_hash != expected_raw_sha256.lower():
        raise ValueError(
            f"raw SHA-256 mismatch: expected {expected_raw_sha256}, got {raw_hash}"
        )
    if expected_prepared_sha256 and prepared_hash != expected_prepared_sha256.lower():
        raise ValueError(
            "prepared SHA-256 mismatch: expected "
            f"{expected_prepared_sha256}, got {prepared_hash}"
        )

    raw = pd.read_csv(raw_path)
    prepared = pd.read_csv(prepared_path)
    if len(raw) != expected_raw_rows:
        raise ValueError(
            f"raw row count mismatch: expected {expected_raw_rows}, got {len(raw)}"
        )

    expected, excluded_rows = _expected_prepared(raw)
    expected_columns = [*FEATURE_COLUMNS, TARGET_COLUMN]
    if prepared.columns.tolist() != expected_columns:
        raise ValueError(
            "prepared columns/order mismatch: expected "
            f"{expected_columns}, got {prepared.columns.tolist()}"
        )
    try:
        pd.testing.assert_frame_equal(
            prepared,
            expected,
            check_dtype=False,
            check_exact=False,
            rtol=1e-12,
            atol=1e-12,
        )
    except AssertionError as error:
        raise ValueError(
            "prepared rows differ from the deterministic full-cohort derivation"
        ) from error

    target_counts = {
        int(label): int(count)
        for label, count in prepared[TARGET_COLUMN].value_counts().sort_index().items()
    }
    if expected_target_counts is not None and target_counts != expected_target_counts:
        raise ValueError(
            "target counts mismatch: expected "
            f"{expected_target_counts}, got {target_counts}"
        )

    return {
        "dataset": "SUPPORT2",
        "source_url": OFFICIAL_SOURCE_URL,
        "source_archive_sha256": archive_hash,
        "source_archive_verified": bool(
            archive_hash is not None and expected_archive_sha256
        ),
        "raw_rows": int(len(raw)),
        "eligible_rows": int(len(expected)),
        "prepared_rows": int(len(prepared)),
        "excluded_rows": excluded_rows,
        "sampling": "none",
        "feature_count": len(FEATURE_COLUMNS),
        "target": TARGET_COLUMN,
        "target_counts": {str(key): value for key, value in target_counts.items()},
        "raw_sha256": raw_hash,
        "prepared_sha256": prepared_hash,
        "rowwise_derivation_match": True,
    }


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="audit-support2")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the official full-dataset audit and print machine-readable proof."""
    args = _parse(argv)
    try:
        result = audit_support2(
            args.raw,
            args.prepared,
            archive_path=args.archive,
        )
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}))
        return 1
    print(json.dumps({"status": "PASSED", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Regression tests for the read-only SUPPORT2 full-dataset audit."""

from pathlib import Path

import pandas as pd
import pytest

from scripts.audit_support2_dataset import (
    FEATURE_COLUMNS,
    TARGET_COLUMN,
    audit_support2,
    file_sha256,
)


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    rows = []
    for index, (death, days) in enumerate(((1, 100), (1, 300), (0, 400))):
        row = {column: float(index + 1) for column in FEATURE_COLUMNS}
        row["dzgroup"] = f"group-{index}"
        row["ca"] = "no"
        row.update({"death": death, "d.time": days})
        rows.append(row)
    raw = pd.DataFrame(rows)
    prepared = raw.loc[:, list(FEATURE_COLUMNS)].copy()
    prepared[TARGET_COLUMN] = [1, 0, 0]
    raw_path = tmp_path / "support2.csv"
    prepared_path = tmp_path / "support2_180d.csv"
    archive_path = tmp_path / "support2csv.zip"
    raw.to_csv(raw_path, index=False)
    prepared.to_csv(prepared_path, index=False)
    archive_path.write_bytes(b"fixture archive")
    return raw_path, prepared_path, archive_path


def test_support2_audit_proves_full_rowwise_derivation(tmp_path: Path) -> None:
    raw_path, prepared_path, archive_path = _write_fixture(tmp_path)

    result = audit_support2(
        raw_path,
        prepared_path,
        archive_path=archive_path,
        expected_archive_sha256=file_sha256(archive_path),
        expected_raw_rows=3,
        expected_raw_sha256=file_sha256(raw_path),
        expected_prepared_sha256=file_sha256(prepared_path),
        expected_target_counts={0: 2, 1: 1},
    )

    assert result["raw_rows"] == 3
    assert result["prepared_rows"] == 3
    assert result["excluded_rows"] == 0
    assert result["sampling"] == "none"
    assert result["source_archive_verified"] is True
    assert result["source_archive_sha256"] == file_sha256(archive_path)
    assert result["rowwise_derivation_match"] is True


def test_support2_audit_rejects_changed_prepared_data(tmp_path: Path) -> None:
    raw_path, prepared_path, archive_path = _write_fixture(tmp_path)
    prepared = pd.read_csv(prepared_path)
    prepared.loc[0, "age"] = 999
    prepared.to_csv(prepared_path, index=False)

    with pytest.raises(ValueError, match="prepared rows differ"):
        audit_support2(
            raw_path,
            prepared_path,
            archive_path=archive_path,
            expected_archive_sha256=file_sha256(archive_path),
            expected_raw_rows=3,
            expected_raw_sha256=file_sha256(raw_path),
            expected_prepared_sha256=file_sha256(prepared_path),
            expected_target_counts={0: 2, 1: 1},
        )

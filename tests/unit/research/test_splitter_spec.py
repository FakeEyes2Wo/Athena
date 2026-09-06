"""Spec-driven splitter interface and edge-case tests."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

from athena.research.splitter import (
    SPLIT_MANIFEST_NAME,
    SplitSpec,
    materialize_csv_split,
    split_ids,
)


def test_split_spec_defaults_match_previous_public_defaults() -> None:
    spec = SplitSpec()

    assert spec.search_frac == 0.2
    assert spec.final_frac == 0.2
    assert spec.seed == 0
    assert spec.group_column is None


def test_group_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="one entry per row id"):
        split_ids(["a", "b", "c"], SplitSpec(), groups=["g1", "g2"])


def test_same_group_never_crosses_train_search_final() -> None:
    ids = [str(i) for i in range(120)]
    groups = [f"g{i // 6}" for i in range(120)]

    manifest = split_ids(
        ids, SplitSpec(search_frac=0.2, final_frac=0.2, seed=3), groups=groups
    )

    by_id = dict(zip(ids, groups))
    train = {by_id[i] for i in manifest.train_ids}
    search = {by_id[i] for i in manifest.search_ids}
    final = {by_id[i] for i in manifest.final_ids}
    assert train & search == set()
    assert train & final == set()
    assert search & final == set()


def test_same_seed_is_deterministic() -> None:
    ids = [str(i) for i in range(100)]
    spec = SplitSpec(search_frac=0.2, final_frac=0.2, seed=42)

    assert split_ids(ids, spec) == split_ids(ids, spec)
    assert split_ids(ids, spec, groups=[f"g{i % 5}" for i in range(100)]) == split_ids(
        ids, spec, groups=[f"g{i % 5}" for i in range(100)]
    )


def test_row_counts_use_row_based_fraction_with_uneven_groups() -> None:
    ids = [str(i) for i in range(120)]
    groups = ["big"] * 60 + [f"g{i}" for i in range(60)]

    manifest = split_ids(
        ids,
        SplitSpec(search_frac=0.25, final_frac=0.25, seed=1),
        groups=groups,
    )

    # Fractions are over rows. A 60-row group may overshoot one split budget,
    # but the split must not collapse to a handful of units.
    assert 25 <= len(manifest.search_ids) <= 90
    assert len(manifest.train_ids) > 0
    assert len(manifest.search_ids) + len(manifest.final_ids) + len(
        manifest.train_ids
    ) == len(ids)


def test_duplicate_row_ids_raise() -> None:
    with pytest.raises(ValueError, match="unique"):
        split_ids(["a", "a", "b"], SplitSpec())


def test_invalid_fraction_combinations_raise() -> None:
    with pytest.raises(ValueError):
        split_ids(["a", "b"], SplitSpec(search_frac=0.6, final_frac=0.6))
    with pytest.raises(ValueError):
        split_ids(["a", "b"], SplitSpec(search_frac=1.0))
    with pytest.raises(ValueError):
        split_ids(["a", "b"], SplitSpec(final_frac=1.0))
    with pytest.raises(ValueError):
        split_ids(["a", "b"], SplitSpec(search_frac=-0.1))


def test_missing_group_column_raises(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text("feature,target\n1,0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="group column"):
        materialize_csv_split(
            source,
            tmp_path / "out",
            "target",
            SplitSpec(group_column="missing"),
        )


def test_split_manifest_records_source_sha256_params_and_output_hashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "windows.csv"
    rows = ["TIC,feature,label"]
    for star in range(20):
        for window in range(6):
            rows.append(f"TIC{star},{window},{window % 2}")
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    out = tmp_path / "split"

    materialize_csv_split(
        source,
        out,
        "label",
        SplitSpec(search_frac=0.2, final_frac=0.2, seed=62, group_column="TIC"),
    )

    manifest = json.loads((out / SPLIT_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert (
        manifest["source_csv"]["sha256"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert manifest["params"] == {
        "target_column": "label",
        "group_column": "TIC",
        "search_frac": 0.2,
        "final_frac": 0.2,
        "seed": 62,
    }
    assert "train.csv" in manifest["files"]
    for name in manifest["files"]:
        assert (
            manifest["files"][name]["sha256"]
            == hashlib.sha256((out / name).read_bytes()).hexdigest()
        )
    # Feature files must not leak the target column.
    with (out / "search_features.csv").open(encoding="utf-8", newline="") as handle:
        assert "label" not in csv.DictReader(handle).fieldnames

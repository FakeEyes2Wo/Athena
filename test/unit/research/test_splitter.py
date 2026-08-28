"""Platform-owned data splitter tests."""

import csv
from pathlib import Path

import pytest

from athena.research.splitter import (
    SplitManifest,
    materialize_csv_split,
    split_ids,
)


def test_split_ids_are_disjoint_complete_and_deterministic() -> None:
    ids = [str(i) for i in range(100)]
    a = split_ids(ids, search_frac=0.2, final_frac=0.2, seed=7)
    b = split_ids(ids, search_frac=0.2, final_frac=0.2, seed=7)

    assert a == b
    assert set(a.train_ids) | set(a.search_ids) | set(a.final_ids) == set(ids)
    assert set(a.train_ids) & set(a.search_ids) == set()
    assert set(a.train_ids) & set(a.final_ids) == set()
    assert set(a.search_ids) & set(a.final_ids) == set()
    assert len(a.train_ids) == 60
    assert len(a.search_ids) == 20
    assert len(a.final_ids) == 20


def test_split_ids_rejects_bad_fractions() -> None:
    with pytest.raises(ValueError):
        split_ids(["a", "b"], search_frac=0.6, final_frac=0.6)
    with pytest.raises(ValueError):
        split_ids(["a", "b"], search_frac=1.0)


def test_materialize_csv_split_writes_platform_files(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text(
        "feature,target\n1,a\n2,b\n3,a\n4,b\n5,a\n6,b\n7,a\n8,b\n9,a\n10,b\n",
        encoding="utf-8",
    )
    out = tmp_path / "split"

    manifest = materialize_csv_split(
        source,
        out,
        "target",
        search_frac=0.2,
        final_frac=0.2,
        seed=0,
    )

    assert isinstance(manifest, SplitManifest)
    assert (out / "train.csv").is_file()
    assert (out / "search_features.csv").is_file()
    assert (out / "search_labels.csv").is_file()
    assert (out / "final_features.csv").is_file()
    assert (out / "final_labels.csv").is_file()

    with (out / "search_features.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["__athena_row_id", "feature"]
        assert all("target" not in row for row in reader)

    with (out / "search_labels.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["__athena_row_id", "target"]


def test_materialize_csv_split_rejects_missing_target(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="target"):
        materialize_csv_split(source, tmp_path / "out", "missing")

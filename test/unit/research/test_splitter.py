"""Platform-owned data splitter tests."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

from athena.research.splitter import (
    SPLIT_MANIFEST_NAME,
    SplitManifest,
    SplitSpec,
    materialize_csv_split,
    split_ids,
)


def test_split_ids_are_disjoint_complete_and_deterministic() -> None:
    ids = [str(i) for i in range(100)]
    spec = SplitSpec(search_frac=0.2, final_frac=0.2, seed=7)
    a = split_ids(ids, spec)
    b = split_ids(ids, spec)

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
        split_ids(["a", "b"], SplitSpec(search_frac=0.6, final_frac=0.6))
    with pytest.raises(ValueError):
        split_ids(["a", "b"], SplitSpec(search_frac=1.0))


def test_split_ids_rejects_duplicate_row_ids() -> None:
    with pytest.raises(ValueError, match="row_ids must be unique"):
        split_ids(["a", "a", "b"], SplitSpec())


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
        SplitSpec(search_frac=0.2, final_frac=0.2, seed=0),
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
        materialize_csv_split(source, tmp_path / "out", "missing", SplitSpec())


# --------------------------------------------------------------------------- #
# grouped splitting
#
# A plain row-level shuffle silently leaks whenever rows are not independent:
# consecutive frames of one active region, windows cut from one star's light
# curve, repeated measurements of one patient. The metric still goes up; it just
# stops meaning anything, and nothing downstream can tell.
# --------------------------------------------------------------------------- #
def test_a_group_never_spans_two_splits() -> None:
    ids = [str(i) for i in range(120)]
    groups = [f"g{i // 6}" for i in range(120)]  # 20 groups of 6 rows

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


def test_grouped_split_still_covers_every_row_exactly_once() -> None:
    ids = [str(i) for i in range(120)]
    groups = [f"g{i // 6}" for i in range(120)]

    manifest = split_ids(
        ids, SplitSpec(search_frac=0.2, final_frac=0.2, seed=3), groups=groups
    )

    assert set(manifest.train_ids) | set(manifest.search_ids) | set(
        manifest.final_ids
    ) == set(ids)
    total = len(manifest.train_ids) + len(manifest.search_ids) + len(manifest.final_ids)
    assert total == len(ids)


def test_grouped_fractions_are_over_rows_not_groups() -> None:
    """组大小不均时按"组数"切会把完全错误的数据量分给每个 split。"""
    # one 60-row group plus 60 single-row groups
    ids = [str(i) for i in range(120)]
    groups = ["big"] * 60 + [f"g{i}" for i in range(60)]

    manifest = split_ids(
        ids,
        SplitSpec(search_frac=0.25, final_frac=0.25, seed=1),
        groups=groups,
    )

    # 25% of 120 rows is 30; the greedy fill may overshoot by at most the size of
    # the one group that crosses the budget, and must never collapse to ~1 unit.
    assert 25 <= len(manifest.search_ids) <= 90
    assert len(manifest.train_ids) > 0


def test_grouped_split_is_deterministic_for_a_seed() -> None:
    ids = [str(i) for i in range(60)]
    groups = [f"g{i // 3}" for i in range(60)]

    spec = SplitSpec(search_frac=0.2, final_frac=0.2, seed=11)
    a = split_ids(ids, spec, groups=groups)
    b = split_ids(ids, spec, groups=groups)

    assert a == b


def test_ungrouped_behaviour_is_unchanged() -> None:
    """没有分组键时必须与旧的纯随机划分逐条一致，否则既有项目的划分会漂移。"""
    ids = [str(i) for i in range(100)]
    spec = SplitSpec(search_frac=0.2, final_frac=0.2, seed=7)

    assert split_ids(ids, spec) == split_ids(ids, spec, groups=None)


def test_split_ids_rejects_mismatched_group_length() -> None:
    with pytest.raises(ValueError, match="one entry per row id"):
        split_ids(["a", "b", "c"], SplitSpec(), groups=["g1", "g2"])


def test_materialize_csv_split_keeps_a_group_column_together(tmp_path: Path) -> None:
    source = tmp_path / "windows.csv"
    rows = ["TIC,feature,label"]
    for star in range(20):
        for window in range(6):
            rows.append(f"TIC{star},{window},{window % 2}")
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    out = tmp_path / "split"

    manifest = materialize_csv_split(
        source,
        out,
        "label",
        SplitSpec(search_frac=0.2, final_frac=0.2, seed=5, group_column="TIC"),
    )

    with source.open(encoding="utf-8", newline="") as handle:
        stars = [row["TIC"] for row in csv.DictReader(handle)]
    owner = {str(index): star for index, star in enumerate(stars)}
    train = {owner[i] for i in manifest.train_ids}
    search = {owner[i] for i in manifest.search_ids}
    final = {owner[i] for i in manifest.final_ids}
    assert train & search == set()
    assert train & final == set()
    assert search & final == set()


def test_materialize_csv_split_rejects_missing_group_column(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text("a,target\n1,0\n2,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="group column"):
        materialize_csv_split(
            source,
            tmp_path / "out",
            "target",
            SplitSpec(group_column="missing"),
        )


def _grouped_source(path: Path) -> Path:
    rows = ["TIC,feature,label"]
    for star in range(20):
        for window in range(6):
            rows.append(f"TIC{star},{window},{window % 2}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_the_split_records_how_it_was_made(tmp_path: Path) -> None:
    """真机 2026-08-30：项目跑完后，没有任何地方写着它是用哪个 seed 划分的。

    参数只活在那一次命令行里；``state.json`` 不存，划分目录里也没有。要把一次
    运行的划分复原出来，只能拿候选 seed 一个个重划、比对 row-id 集合，直到
    seed=62 命中——而这还得指望源 CSV 一个字节没变过。
    """
    source = _grouped_source(tmp_path / "windows.csv")
    out = tmp_path / "split"

    materialize_csv_split(
        source,
        out,
        "label",
        SplitSpec(search_frac=0.2, final_frac=0.2, seed=62, group_column="TIC"),
    )

    manifest = json.loads((out / SPLIT_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["params"] == {
        "target_column": "label",
        "group_column": "TIC",
        "search_frac": 0.2,
        "final_frac": 0.2,
        "seed": 62,
    }
    assert manifest["source_csv"]["path"] == str(source.resolve())
    assert manifest["source_csv"]["sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    # Every split file it wrote is listed, with the hash it had when written --
    # so a later "the numbers moved" can be told apart from "the data moved".
    assert set(manifest["files"]) == {
        "train.csv",
        "search_features.csv",
        "search_labels.csv",
        "final_features.csv",
        "final_labels.csv",
    }
    for name, entry in manifest["files"].items():
        assert entry["sha256"] == hashlib.sha256((out / name).read_bytes()).hexdigest()


def test_the_manifest_is_enough_to_replay_the_split(tmp_path: Path) -> None:
    """记录的意义在于能照着它重划一遍，而不是只留个好看的 JSON。"""
    source = _grouped_source(tmp_path / "windows.csv")
    original = tmp_path / "original"
    materialize_csv_split(
        source,
        original,
        "label",
        SplitSpec(search_frac=0.2, final_frac=0.2, seed=62, group_column="TIC"),
    )
    manifest = json.loads(
        (original / SPLIT_MANIFEST_NAME).read_text(encoding="utf-8")
    )

    replayed = tmp_path / "replayed"
    params = manifest["params"]
    materialize_csv_split(
        Path(manifest["source_csv"]["path"]),
        replayed,
        params["target_column"],
        SplitSpec(
            search_frac=params["search_frac"],
            final_frac=params["final_frac"],
            seed=params["seed"],
            group_column=params["group_column"],
        ),
    )

    for name, entry in manifest["files"].items():
        assert (
            hashlib.sha256((replayed / name).read_bytes()).hexdigest()
            == entry["sha256"]
        ), f"{name} did not replay byte for byte"

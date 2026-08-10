"""DatasetService 不可变/derived 校验测试（supervisor_imp_docs Task 4）。

覆盖 ingest 的字节级 SHA-256 manifest（不解析业务数据），以及 accept_derived 对
列/类型/逐值 hash/行身份/split membership 不变量的强制：任何原始不变量变化即
OriginalColumnMutation，追加新列允许。
"""

from pathlib import Path

import pytest

from athena.research.contracts import DatasetManifest, DerivedDatasetManifest
from athena.research.data_service import DatasetService, OriginalColumnMutation


def _write_csv(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _original(tmp_path: Path) -> Path:
    src = tmp_path / "original"
    _write_csv(src / "train.csv", "id,age,label\n1,20,0\n2,30,1\n")
    _write_csv(src / "test.csv", "id,age,label\n3,25,0\n")
    return src


def _parent(**overrides) -> DatasetManifest:
    base = dict(
        manifest_id="data_1",
        source_root="original",
        files={"train.csv": "sha:train", "test.csv": "sha:test"},
        columns={"age": "int64", "label": "int64"},
        column_hashes={"age": "hash:age:1", "label": "hash:label"},
        row_identity_hash="hash:rows",
        split_boundaries={"train": {"rows": 2}},
    )
    return DatasetManifest(**{**base, **overrides})


def _derived(parent: DatasetManifest, **overrides) -> DerivedDatasetManifest:
    base = dict(
        manifest_id="derived_1",
        parent_manifest_id=parent.manifest_id,
        files={},
        columns=parent.columns,
        column_hashes=parent.column_hashes,
        row_identity_hash=parent.row_identity_hash,
        split_boundaries=parent.split_boundaries,
        derived_columns=[],
    )
    return DerivedDatasetManifest(**{**base, **overrides})


def test_ingest_hashes_files_without_parsing(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    manifest = service.ingest(_original(tmp_path))
    assert set(manifest.files) == {"train.csv", "test.csv"}
    assert all(len(value) == 64 for value in manifest.files.values())  # sha256 hex
    assert manifest.columns == {}  # 不解析业务数据，列不变量留待 LLM 登记
    # 再次 ingest 得到相同 hash（字节不变）
    assert service.ingest(_original(tmp_path)).files == manifest.files


def test_derived_cannot_change_original_column_value(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(
        parent, column_hashes={"age": "hash:age:2", "label": "hash:label"}
    )
    with pytest.raises(OriginalColumnMutation):
        service.accept_derived(parent, candidate)


def test_derived_cannot_change_original_column_type(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(parent, columns={"age": "float64", "label": "int64"})
    with pytest.raises(OriginalColumnMutation):
        service.accept_derived(parent, candidate)


def test_derived_cannot_change_row_identity(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(parent, row_identity_hash="hash:rows:2")
    with pytest.raises(OriginalColumnMutation):
        service.accept_derived(parent, candidate)


def test_derived_cannot_change_split_membership(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(parent, split_boundaries={"train": {"rows": 3}})
    with pytest.raises(OriginalColumnMutation):
        service.accept_derived(parent, candidate)


def test_derived_rejects_changed_original_file_bytes(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(parent, files={"train.csv": "sha:train:CHANGED"})
    with pytest.raises(OriginalColumnMutation):
        service.accept_derived(parent, candidate)


def test_derived_rejects_new_column_conflicting_with_original(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(parent, derived_columns=["age"])
    with pytest.raises(OriginalColumnMutation):
        service.accept_derived(parent, candidate)


def test_derived_allows_appending_new_columns(tmp_path) -> None:
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(
        parent,
        columns={**parent.columns, "new_feature": "float64"},
        column_hashes={**parent.column_hashes, "new_feature": "hash:new"},
        files={"derived_features.csv": "sha:derived"},
        derived_columns=["new_feature"],
    )
    result = service.accept_derived(parent, candidate)
    assert result.derived_columns == ["new_feature"]
    assert result.parent_manifest_id == parent.manifest_id

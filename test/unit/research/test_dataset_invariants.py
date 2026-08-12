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
        files={**parent.files, "features/derived.csv": "sha:derived"},
        derived_columns=["new_feature"],
        column_files={"new_feature": "features/derived.csv"},
        enabled_derived_columns=["new_feature"],
    )
    result = service.accept_derived(parent, candidate)
    assert result.derived_columns == ["new_feature"]
    assert result.column_files == {"new_feature": "features/derived.csv"}
    assert result.parent_manifest_id == parent.manifest_id


# ---- 增量特征视图（supervisor-incremental-feature-dataset §Invariants）----


def test_derived_rejects_derived_column_without_file_mapping(tmp_path) -> None:
    """不变量 4：每个 derived column 必须映射到已记录增量文件。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(parent, derived_columns=["orphan"])
    with pytest.raises(OriginalColumnMutation, match="no file mapping"):
        service.accept_derived(parent, candidate)


def test_derived_rejects_enabled_column_not_in_derived(tmp_path) -> None:
    """不变量 5：enabled derived column 必须 ∈ derived_columns。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    candidate = _derived(
        parent,
        files={**parent.files, "features/new.csv": "sha:new"},
        derived_columns=["feat"],
        column_files={"feat": "features/new.csv"},
        enabled_derived_columns=["ghost"],
    )
    with pytest.raises(OriginalColumnMutation, match="enabled derived column"):
        service.accept_derived(parent, candidate)


def test_derived_previous_incremental_file_hash_must_match(tmp_path) -> None:
    """不变量 2：previous 增量文件 hash 保持不变（重放/追加时）。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    previous = _derived(
        parent,
        files={**parent.files, "features/prev.csv": "sha:prev"},
        derived_columns=["f1"],
        column_files={"f1": "features/prev.csv"},
    )
    previous = service.accept_derived(parent, previous)
    candidate = _derived(
        parent,
        files={
            **parent.files,
            "features/prev.csv": "sha:CHANGED",
            "features/new.csv": "sha:new",
        },
        derived_columns=["f1", "f2"],
        column_files={"f1": "features/prev.csv", "f2": "features/new.csv"},
    )
    with pytest.raises(OriginalColumnMutation, match="derived file changed"):
        service.accept_derived(parent, candidate, previous=previous)


def test_derived_disabling_feature_keeps_metadata(tmp_path) -> None:
    """不变量 3/6：禁用特征不删文件/元数据（derived_columns 与 files 保留，manifest_id 变）。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()
    previous = _derived(
        parent,
        files={
            **parent.files,
            "features/f1.csv": "sha:f1",
            "features/f2.csv": "sha:f2",
        },
        derived_columns=["f1", "f2"],
        column_files={"f1": "features/f1.csv", "f2": "features/f2.csv"},
        enabled_derived_columns=["f1", "f2"],
    )
    previous = service.accept_derived(parent, previous)
    candidate = _derived(
        parent,
        files=previous.files,
        derived_columns=previous.derived_columns,
        column_files=previous.column_files,
        enabled_derived_columns=["f1"],  # 禁用 f2
    )
    accepted = service.accept_derived(parent, candidate, previous=previous)
    assert accepted.enabled_derived_columns == ["f1"]
    assert accepted.derived_columns == ["f1", "f2"]  # 元数据不删
    assert accepted.files == previous.files  # 文件不删
    assert accepted.manifest_id != previous.manifest_id  # 新视图（不同 id）


def test_derived_manifest_id_is_deterministic(tmp_path) -> None:
    """不变量 7：同批次重放 → 同 manifest_id（幂等）。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent()

    def build() -> DerivedDatasetManifest:
        return _derived(
            parent,
            files={**parent.files, "features/new.csv": "sha:new"},
            derived_columns=["feat"],
            column_files={"feat": "features/new.csv"},
            enabled_derived_columns=["feat"],
        )

    a = service.accept_derived(parent, build())
    b = service.accept_derived(parent, build())
    assert a.manifest_id == b.manifest_id
    assert a.manifest_id.startswith("view_")


# ---- 视图解析（supervisor-incremental-feature-dataset §Resolving A View）----


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _derived_view(
    parent: DatasetManifest, derived: Path, **overrides
) -> DerivedDatasetManifest:
    """构造带真实增量文件 hash 的视图（derived 文件需先写入 derived_root）。"""
    base_files = {**parent.files}
    column_files: dict[str, str] = {}
    derived_columns: list[str] = []
    for name, rel in overrides.pop("increments", {}).items():
        base_files[rel] = _sha(derived / rel)
        column_files[name] = rel
        derived_columns.append(name)
    params = {
        "files": base_files,
        "derived_columns": derived_columns,
        "column_files": column_files,
        "enabled_derived_columns": list(derived_columns),
    }
    params.update(overrides)  # 显式覆盖（如只 enable 部分列）
    return _derived(parent, **params)


def test_resolve_view_assembles_original_and_enabled(tmp_path) -> None:
    """resolve_view 组装原始文件 + enabled 增量文件（derived/ 命名空间）；disabled 不组装。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = service.ingest(_original(tmp_path))
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_csv(derived / "features/a.csv", "id,feat_a\n1,10\n2,20\n3,30\n")
    _write_csv(derived / "features/b.csv", "id,feat_b\n1,1\n2,1\n3,0\n")
    view = _derived_view(
        parent,
        derived,
        increments={"feat_a": "features/a.csv", "feat_b": "features/b.csv"},
        enabled_derived_columns=["feat_a"],  # feat_b disabled
    )
    workspace = service.resolve_view(parent, view, derived_root=derived)
    assert (workspace / "train.csv").is_file()
    assert (workspace / "test.csv").is_file()
    assert (workspace / "derived" / "features" / "a.csv").is_file()
    assert not (
        workspace / "derived" / "features" / "b.csv"
    ).exists()  # disabled 不组装


def test_resolve_view_rejects_missing_incremental(tmp_path) -> None:
    """增量文件缺失 → 解析失败（不留部分视图）。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = service.ingest(_original(tmp_path))
    derived = tmp_path / "derived"
    derived.mkdir()
    view = _derived(
        parent,
        files={**parent.files, "features/a.csv": "sha:a"},
        derived_columns=["feat_a"],
        column_files={"feat_a": "features/a.csv"},
        enabled_derived_columns=["feat_a"],
    )
    with pytest.raises(OriginalColumnMutation, match="derived file missing"):
        service.resolve_view(parent, view, derived_root=derived)


def test_resolve_view_rejects_changed_incremental_hash(tmp_path) -> None:
    """增量文件 hash 不符 → 解析失败。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = service.ingest(_original(tmp_path))
    derived = tmp_path / "derived"
    derived.mkdir()
    _write_csv(derived / "features/a.csv", "id,feat_a\n1,10\n")
    view = _derived(
        parent,
        files={**parent.files, "features/a.csv": "sha:WRONG"},
        derived_columns=["feat_a"],
        column_files={"feat_a": "features/a.csv"},
        enabled_derived_columns=["feat_a"],
    )
    with pytest.raises(OriginalColumnMutation, match="derived file changed"):
        service.resolve_view(parent, view, derived_root=derived)


def test_resolve_view_rejects_missing_original_snapshot(tmp_path) -> None:
    """原始受管快照缺失 → 解析失败。"""
    service = DatasetService(workdir=tmp_path / "work")
    parent = _parent(managed_root=str(tmp_path / "nope"))
    view = _derived(parent)
    with pytest.raises(OriginalColumnMutation, match="managed snapshot"):
        service.resolve_view(parent, view, derived_root=tmp_path)

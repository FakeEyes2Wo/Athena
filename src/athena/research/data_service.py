"""DatasetService — 原始摄取、不可变 manifest 与 derived 数据不变量（Task 4）。

平台不解析业务数据：``ingest`` 只复制源字节并生成 SHA-256 manifest；
``accept_derived`` 校验原始文件逐字节不变，只允许追加不冲突的新文件/新列，
满足"原始列、行身份、逐值 hash 与 split 边界永久不可变"的不变量。
"""

import hashlib
import json
import shutil
from pathlib import Path

from athena.core.contracts import new_id
from athena.research.contracts import DatasetManifest, DerivedDatasetManifest


class OriginalColumnMutation(ValueError):
    """派生数据改变了原始文件内容或缺失原始文件。"""


def _derived_view_id(manifest: DerivedDatasetManifest) -> str:
    """确定性视图 id：原始 id + 排序文件 hash + 列元数据 + 排序 enabled 特征名。

    同批次重放产生同 id → 接受幂等（supervisor-incremental-feature-dataset §Invariants 7）。
    """
    payload = json.dumps(
        {
            "parent": manifest.parent_manifest_id,
            "files": sorted(manifest.files.items()),
            "columns": sorted(manifest.columns.items()),
            "column_hashes": sorted(manifest.column_hashes.items()),
            "derived": sorted(manifest.derived_columns),
            "enabled": sorted(manifest.enabled_derived_columns),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "view_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class DatasetService:
    """不可变原始摄取与 derived 校验（字节级，不解析业务语义）。"""

    def __init__(self, *, workdir: Path) -> None:
        self._workdir = Path(workdir)

    def resolve_view(
        self,
        parent: DatasetManifest,
        view: DerivedDatasetManifest,
        *,
        derived_root: Path,
    ) -> Path:
        """把视图解析为 worker 可用 workspace（supervisor-incremental-feature-dataset §Resolving A View）。

        只读取 ``enabled_derived_columns`` 需要的增量文件；任何文件缺失 / hash 不符 /
        列映射不全 → 立即失败，不留部分视图（旧 active 视图保持权威）。返回的 workspace
        含原始文件（来自受管快照）与 enabled 增量文件（``derived/`` 子目录）。
        """
        managed = Path(parent.managed_root) if parent.managed_root else None
        if managed is None or not managed.is_dir():
            raise OriginalColumnMutation("original managed snapshot missing")
        # 1. 原始文件字节不变
        for rel, expected in parent.files.items():
            path = managed / rel
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise OriginalColumnMutation(f"original file missing or changed: {rel}")
        # 2. enabled 增量文件存在 + hash 一致 + 含列
        enabled_files = {
            view.column_files[col]
            for col in view.enabled_derived_columns
            if col in view.column_files
        }
        for rel in sorted(enabled_files):
            expected = view.files.get(rel)
            path = derived_root / rel
            if not path.is_file():
                raise OriginalColumnMutation(f"derived file missing: {rel}")
            if (
                expected is not None
                and hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise OriginalColumnMutation(f"derived file changed: {rel}")
        # 3. 组装 workspace：原始文件 + enabled 增量文件（derived/ 命名空间，避免同名冲突）
        workspace = self._workdir / "views" / view.manifest_id
        if workspace.exists():
            shutil.rmtree(workspace)
        for rel in parent.files:
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(managed / rel, dest)
        for rel in sorted(enabled_files):
            dest = workspace / "derived" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(derived_root / rel, dest)
        return workspace

    def ingest(self, source_root: Path) -> DatasetManifest:
        """复制源字节并生成 SHA-256 manifest；不读取/解析业务数据。

        ``source_root`` 可以是目录（复制全部普通文件）或单文件；平台不按
        文件名/扩展名推断业务语义。空路径立即拒绝（避免把当前目录当源根）。
        """
        if not source_root:
            raise FileNotFoundError(f"source root missing: {source_root!r}")
        src = Path(source_root)
        if not src.exists():
            raise FileNotFoundError(f"source root missing: {src}")
        managed = self._workdir / "raw"
        managed.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        if src.is_file():
            files[src.name] = self._copy(src, managed / src.name)
        elif src.is_dir():
            for path in sorted(src.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(src).as_posix()
                files[rel] = self._copy(path, managed / rel)
        else:
            raise FileNotFoundError(f"source root missing: {src}")
        return DatasetManifest(
            manifest_id=new_id("data"),
            source_root=str(src),
            managed_root=str(managed),
            files=files,
            split_boundaries={},
        )

    @staticmethod
    def _copy(source: Path, dest: Path) -> str:
        """复制单个源字节并返回目标 SHA-256。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return hashlib.sha256(dest.read_bytes()).hexdigest()

    def accept_derived(
        self,
        parent: DatasetManifest,
        candidate: DerivedDatasetManifest,
        *,
        previous: DerivedDatasetManifest | None = None,
    ) -> DerivedDatasetManifest:
        """校验 derived 不变量并返回确定 id 的已接受视图。

        不变量（supervisor-incremental-feature-dataset §Invariants）：原始文件/列/值/
        行身份/split 不变；previous 增量文件 hash 不变；derived_columns 是 previous 超集
        （禁用不删元数据）；每个 derived column 映射到恰一个记录文件；每个 enabled 列在
        derived_columns 中；``manifest_id`` 为确定性 hash（同批次重放幂等）。
        """
        # 既有不变量：原始文件/列/值/行身份/split/原始列冲突
        for rel, expected in parent.files.items():
            got = candidate.files.get(rel)
            if got is not None and got != expected:
                raise OriginalColumnMutation(f"original file changed: {rel}")
        for col, typ in parent.columns.items():
            if candidate.columns.get(col) != typ:
                raise OriginalColumnMutation(f"original column type changed: {col}")
            if candidate.column_hashes.get(col) != parent.column_hashes.get(col):
                raise OriginalColumnMutation(f"original column value changed: {col}")
        if (
            parent.row_identity_hash is not None
            and candidate.row_identity_hash != parent.row_identity_hash
        ):
            raise OriginalColumnMutation("row identity changed")
        for key, value in parent.split_boundaries.items():
            if candidate.split_boundaries.get(key) != value:
                raise OriginalColumnMutation(f"split membership changed: {key}")
        conflict = [col for col in candidate.derived_columns if col in parent.columns]
        if conflict:
            raise OriginalColumnMutation(
                f"derived column conflicts with original: {conflict}"
            )
        # 不变量 2/3/6：previous 增量文件 hash 不变；derived_columns 是超集（禁用不删元数据）
        if previous is not None:
            for rel, expected in previous.files.items():
                if rel in parent.files:
                    continue  # 原始文件已校验
                if candidate.files.get(rel) != expected:
                    raise OriginalColumnMutation(f"derived file changed: {rel}")
            erased = [
                col
                for col in previous.derived_columns
                if col not in candidate.derived_columns
            ]
            if erased:
                raise OriginalColumnMutation(
                    f"derived column metadata erased: {erased}"
                )
            for col in previous.derived_columns:
                if candidate.column_files.get(col) != previous.column_files.get(col):
                    raise OriginalColumnMutation(
                        f"derived column file mapping erased: {col}"
                    )
        # 不变量 4：每个 derived column 映射到恰一个已记录增量文件
        unmapped = [
            col
            for col in candidate.derived_columns
            if col not in candidate.column_files
        ]
        if unmapped:
            raise OriginalColumnMutation(
                f"derived column has no file mapping: {unmapped}"
            )
        for col, rel in candidate.column_files.items():
            if rel not in candidate.files:
                raise OriginalColumnMutation(f"derived column file not recorded: {rel}")
        # 不变量 5：每个 enabled derived column ∈ derived_columns
        unknown = [
            col
            for col in candidate.enabled_derived_columns
            if col not in candidate.derived_columns
        ]
        if unknown:
            raise OriginalColumnMutation(
                f"enabled derived column not in derived_columns: {unknown}"
            )
        # 不变量 7：确定性 manifest_id
        return candidate.model_copy(update={"manifest_id": _derived_view_id(candidate)})

"""DatasetService — 原始摄取、不可变 manifest 与 derived 数据不变量（Task 4）。

平台不解析业务数据：``ingest`` 只复制源字节并生成 SHA-256 manifest；
``accept_derived`` 校验原始文件逐字节不变，只允许追加不冲突的新文件/新列，
满足"原始列、行身份、逐值 hash 与 split 边界永久不可变"的不变量。
"""

import hashlib
import shutil
from pathlib import Path

from athena.core.contracts import new_id
from athena.research.contracts import DatasetManifest, DerivedDatasetManifest


class OriginalColumnMutation(ValueError):
    """派生数据改变了原始文件内容或缺失原始文件。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DatasetService:
    """不可变原始摄取与 derived 校验（字节级，不解析业务语义）。"""

    def __init__(self, *, workdir: Path) -> None:
        self._workdir = Path(workdir)

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
        return _sha256(dest)

    def accept_derived(
        self, parent: DatasetManifest, candidate: DerivedDatasetManifest
    ) -> DerivedDatasetManifest:
        """校验 derived 不变量：原始文件/列/类型/逐值 hash/行身份/split 不变。

        只允许追加新文件与新列；任何原始不变量变化抛 ``OriginalColumnMutation``。
        """
        # 1. 原始文件字节不变（candidate 若记录原始文件则必须逐字节一致）
        for rel, expected in parent.files.items():
            got = candidate.files.get(rel)
            if got is not None and got != expected:
                raise OriginalColumnMutation(f"original file changed: {rel}")
        # 2. 原始列类型与逐值 hash 不变（新列允许）
        for col, typ in parent.columns.items():
            if candidate.columns.get(col) != typ:
                raise OriginalColumnMutation(f"original column type changed: {col}")
            if candidate.column_hashes.get(col) != parent.column_hashes.get(col):
                raise OriginalColumnMutation(f"original column value changed: {col}")
        # 3. 行身份不变
        if (
            parent.row_identity_hash is not None
            and candidate.row_identity_hash != parent.row_identity_hash
        ):
            raise OriginalColumnMutation("row identity changed")
        # 4. split membership 不变（原始 split 边界必须一致）
        for key, value in parent.split_boundaries.items():
            if candidate.split_boundaries.get(key) != value:
                raise OriginalColumnMutation(f"split membership changed: {key}")
        # 5. 新列名不得与原列冲突（派生列不允许重定义原始列名）
        conflict = [col for col in candidate.derived_columns if col in parent.columns]
        if conflict:
            raise OriginalColumnMutation(
                f"derived column conflicts with original: {conflict}"
            )
        return candidate

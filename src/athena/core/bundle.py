"""内容寻址目录 Bundle。

正式 DataAnalysis 版本表现为不可变逻辑目录：:

    DataAnalysis/
      report.md
      figures/...

文件先分别写入 ArtifactStore，最后原子写入 Manifest（自身也是 Artifact），其
ArtifactRef 即版本引用。Manifest 是存储层内部 schema，不进入 Agent 公共合同。

Manifest 结构::

    {
      "schema_version": 1,
      "kind": "directory",
      "files": {"report.md": "sha256:...", "figures/a.png": "sha256:..."},
      "parent_ref": "sha256:..." | null
    }
"""

import json
import re
from uuid import uuid4
from typing import Any

from athena.core.contracts import ArtifactRef, ArtifactStore

_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


class InvalidBundleError(ValueError):
    """文件集合或 Manifest 不满足目录 Bundle 的结构约束。"""


class VersionedBundleError(ValueError):
    """版本所有权链违反提交约束。"""


class UnknownAnalysisError(VersionedBundleError):
    """analysis_id 不存在于版本链。"""


class OwnershipError(VersionedBundleError):
    """提交者不是该分析链的 owner。"""


class StaleParentError(VersionedBundleError):
    """parent_ref 不等于当前 latest_ref。"""


class DirectoryBundle:
    """逻辑目录 Bundle 的提交与读取（存储层内部实现）。"""

    @staticmethod
    async def commit(
        store: ArtifactStore,
        files: dict[str, ArtifactRef],
        parent_ref: ArtifactRef | None = None,
    ) -> ArtifactRef:
        """规范化校验路径并写入 Manifest，返回其内容引用。

        只保证相对路径合法且非空；DataAnalysis 的 report/figures 结构要求由
        :func:`validate_data_analysis` 单独校验。校验失败零副作用——不写入
        Manifest，之前的单文件 Artifact 只是暂存内容。
        """
        normalized = {_normalize_path(p): ref for p, ref in files.items()}
        if not normalized:
            raise InvalidBundleError("bundle files must be non-empty")
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "kind": "directory",
            "files": normalized,
            "parent_ref": parent_ref,
        }
        return await store.put_text(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        )

    @staticmethod
    async def files(
        store: ArtifactStore, manifest_ref: ArtifactRef
    ) -> dict[str, ArtifactRef]:
        """解析 Manifest，返回 ``相对路径 -> 内容引用``。"""
        manifest = json.loads(await store.get_text(manifest_ref))
        if manifest.get("kind") != "directory":
            raise InvalidBundleError(f"not a directory manifest: {manifest_ref}")
        return dict(manifest["files"])

    @staticmethod
    async def read(
        store: ArtifactStore, manifest_ref: ArtifactRef, rel_path: str
    ) -> bytes:
        """读取 Bundle 内相对路径文件的内容。"""
        files = await DirectoryBundle.files(store, manifest_ref)
        ref = files.get(_normalize_path(rel_path))
        if ref is None:
            raise KeyError(f"no such file in bundle: {rel_path}")
        return await store.get_bytes(ref)

    @staticmethod
    async def validate_report_references(
        store: ArtifactStore, manifest_ref: ArtifactRef
    ) -> None:
        """report.md 中每个本地图片引用必须解析到同一 Bundle 内文件（设计 §7.4）。

        外部 URL 与 data URI 不校验；绝对路径、``..`` 穿越与 Bundle 内缺失的引用
        一律拒绝。结构校验只证明报告完整可读，不评价图片质量。
        """
        files = await DirectoryBundle.files(store, manifest_ref)
        report_ref = files.get("report.md")
        if report_ref is None:
            raise InvalidBundleError("bundle has no report.md")
        report = await store.get_text(report_ref)
        for raw in _IMAGE_REF_RE.findall(report):
            url = raw.split(" ")[0].strip("<>").strip()
            if not url or "://" in url or url.startswith("data:"):
                continue  # 外部/数据 URL → 非 Bundle 内引用，跳过
            path = _normalize_path(url)  # 绝对路径 / .. 穿越 → InvalidBundleError
            if path not in files:
                raise InvalidBundleError(f"image reference not in bundle: {url}")


def _normalize_path(path: str) -> str:
    """规范化正斜杠相对路径；绝对路径、``..`` 穿越与空段直接拒绝。"""
    if not isinstance(path, str) or not path:
        raise InvalidBundleError(f"invalid bundle path: {path!r}")
    if path.startswith("/") or "\\" in path or ":" in path:
        raise InvalidBundleError(f"invalid bundle path: {path!r}")
    parts = path.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        raise InvalidBundleError(f"invalid bundle path: {path!r}")
    return "/".join(parts)


async def validate_data_analysis(
    store: ArtifactStore, manifest_ref: ArtifactRef
) -> None:
    """DataAnalysis 结构校验：恰一个根 ``report.md``、至少一张 ``figures/`` 图片。

    只证明报告完整可读，不评价图片是否必要或支持结论（语义质量由 Reflection 评分）。
    """
    files = await DirectoryBundle.files(store, manifest_ref)
    if sum(1 for p in files if p == "report.md") != 1:
        raise InvalidBundleError("bundle must contain exactly one root report.md")
    if not any(p.startswith("figures/") for p in files):
        raise InvalidBundleError("bundle must contain at least one figures/ image")


class VersionedBundle:
    """DataAnalysis 版本所有权链。

    ``analysis_id -> (owner_agent_id, latest_ref)`` 是存储层事实，不进入公共 DTO。
    后续提交必须由同一 owner 发起且 ``parent_ref == latest_ref``；校验成功并写入
    新 Manifest 后才原子更新 ``latest_ref``。首版为内存实现。
    """

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store
        self._owners: dict[str, str] = {}
        self._latest: dict[str, ArtifactRef] = {}

    def owner(self, analysis_id: str) -> str | None:
        """返回分析链的 owner；不存在返回 None。"""
        return self._owners.get(analysis_id)

    def latest(self, analysis_id: str) -> ArtifactRef | None:
        """返回分析链的最新版本引用；不存在返回 None。"""
        return self._latest.get(analysis_id)

    def chains(self) -> dict[str, ArtifactRef]:
        """analysis_id -> latest_ref 的只读快照。"""
        return dict(self._latest)

    async def create(
        self, owner_agent_id: str, files: dict[str, ArtifactRef]
    ) -> tuple[str, ArtifactRef]:
        """首次提交 v1：创建新分析链并返回 (analysis_id, ref)。"""
        ref = await self._commit_version(files, None)
        analysis_id = f"analysis_{uuid4().hex[:8]}"
        self._owners[analysis_id] = owner_agent_id
        self._latest[analysis_id] = ref
        return analysis_id, ref

    async def commit(
        self,
        analysis_id: str,
        owner_agent_id: str,
        files: dict[str, ArtifactRef],
        parent_ref: ArtifactRef,
    ) -> ArtifactRef:
        """提交新版本：校验 owner 与 parent_ref==latest，成功则更新 latest_ref。"""
        if analysis_id not in self._latest:
            raise UnknownAnalysisError(f"unknown analysis chain: {analysis_id}")
        latest = self._latest[analysis_id]
        if self._owners[analysis_id] != owner_agent_id:
            raise OwnershipError(f"{owner_agent_id} is not the owner of {analysis_id}")
        if parent_ref != latest:
            raise StaleParentError(
                f"parent_ref does not match latest for {analysis_id}"
            )
        ref = await self._commit_version(files, parent_ref)
        self._latest[analysis_id] = ref  # Manifest 写入且校验通过后才更新
        return ref

    async def _commit_version(
        self, files: dict[str, ArtifactRef], parent_ref: ArtifactRef | None
    ) -> ArtifactRef:
        """写入 Manifest 并校验 DataAnalysis 结构；校验失败不更新 latest_ref。"""
        ref = await DirectoryBundle.commit(self._store, files, parent_ref)
        await validate_data_analysis(self._store, ref)
        return ref

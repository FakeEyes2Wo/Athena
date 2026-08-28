"""数据集分发：把一份大数据送到 GPU 机上，并且**证明它是完整的**。"""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from athena.execution.remote.channel import RemoteChannel
from athena.execution.remote.mirror import FileEntry, local_manifest

logger = logging.getLogger(__name__)

COMPLETE_MARKER = ".athena-complete.json"

# 数据集目录里不参与哈希、也不上传的名字。
_DATASET_EXCLUDES: frozenset[str] = frozenset(
    {".git", ".venv", "__pycache__", ".ipynb_checkpoints", COMPLETE_MARKER}
)


class DatasetError(RuntimeError):
    """数据集不完整、对不上，或者压根没送到。"""


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """一份数据集的身份：清单 + 由清单算出的 id。"""

    dataset_id: str
    entries: tuple[FileEntry, ...]

    @property
    def total_bytes(self) -> int:
        """全部文件的字节数（用于估时与容量回收）。"""
        return sum(entry.size for entry in self.entries)

    def marker_payload(self) -> dict:
        """写进 ``.complete`` 的内容——足够事后独立复核。"""
        return {
            "dataset_id": self.dataset_id,
            "files": len(self.entries),
            "bytes": self.total_bytes,
        }


def describe_dataset(root: Path) -> DatasetSpec:
    """给本地数据集算清单与 id。"""
    entries = local_manifest(Path(root), excludes=_DATASET_EXCLUDES)
    if not entries:
        raise DatasetError(f"dataset root has no files: {root}")
    ordered = tuple(entries[key] for key in sorted(entries))
    digest = hashlib.sha256()
    for entry in ordered:
        digest.update(f"{entry.path}:{entry.size}:{entry.sha256}\n".encode())
    return DatasetSpec(dataset_id=digest.hexdigest()[:32], entries=ordered)


@dataclass(frozen=True, slots=True)
class StageReport:
    """一次分发的结果。"""

    dataset_id: str
    remote_root: str
    uploaded: tuple[str, ...]
    bytes_sent: int
    reused: bool

    @property
    def resumed(self) -> bool:
        """是否是续传（远端已有一部分，但没有完成标记）。"""
        return bool(self.uploaded) and not self.reused


class DatasetStager:
    """把数据集分发到一台机器的 scratch 下并保证完整。"""

    def __init__(self, channel: RemoteChannel, *, data_root: str) -> None:
        self._channel = channel
        self._root = PurePosixPath(data_root)

    def remote_root(self, spec: DatasetSpec) -> str:
        """这份数据在远端的目录（内容寻址）。"""
        return str(self._root / spec.dataset_id)

    async def _marker(self, spec: DatasetSpec) -> dict | None:
        """读完成标记；不存在或损坏都当作"没有"。"""
        path = f"{self.remote_root(spec)}/{COMPLETE_MARKER}"
        try:
            raw = await self._channel.read_file(path)
        except Exception:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _remote_entries(self, spec: DatasetSpec) -> dict[str, FileEntry]:
        reply = await self._channel.request(
            "manifest",
            root=self.remote_root(spec),
            exclude=sorted(_DATASET_EXCLUDES),
        )
        return {
            row[0]: FileEntry(row[0], int(row[1]), str(row[2]))
            for row in reply.get("entries", [])
        }

    async def verify(self, spec: DatasetSpec) -> None:
        """复核远端那份数据仍与清单逐字节一致；不一致就抛错。"""
        remote = await self._remote_entries(spec)
        problems: list[str] = []
        for entry in spec.entries:
            found = remote.get(entry.path)
            if found is None:
                problems.append(f"missing {entry.path}")
            elif found.sha256 != entry.sha256:
                problems.append(f"content differs {entry.path}")
            if len(problems) >= 5:
                break
        if problems:
            raise DatasetError(
                f"staged dataset {spec.dataset_id} does not match the local manifest: "
                + "; ".join(problems)
            )

    async def stage(
        self, local_root: Path, spec: DatasetSpec, *, verify_existing: bool = True
    ) -> StageReport:
        """把数据集送到远端；已完整就复用，未完整就续传。"""
        remote_root = self.remote_root(spec)
        marker = await self._marker(spec)
        if marker is not None and marker.get("dataset_id") == spec.dataset_id:
            if verify_existing:
                await self.verify(spec)
            logger.info("dataset %s already staged at %s", spec.dataset_id, remote_root)
            return StageReport(spec.dataset_id, remote_root, (), 0, reused=True)

        await self._channel.request("mkdir", path=remote_root)
        remote = await self._remote_entries(spec)
        missing = [
            entry
            for entry in spec.entries
            if (found := remote.get(entry.path)) is None or found.sha256 != entry.sha256
        ]
        uploaded = [entry.path for entry in missing]
        sent = sum(entry.size for entry in missing)
        root = Path(local_root)
        await self._channel.send_files(
            (f"{remote_root}/{entry.path}", root / entry.path) for entry in missing
        )

        await self.verify(spec)
        await self._channel.write_file(
            f"{remote_root}/{COMPLETE_MARKER}",
            json.dumps(spec.marker_payload(), ensure_ascii=False).encode("utf-8"),
        )
        logger.info(
            "staged dataset %s to %s (%d files, %d bytes)",
            spec.dataset_id,
            remote_root,
            len(uploaded),
            sent,
        )
        return StageReport(
            spec.dataset_id, remote_root, tuple(uploaded), sent, reused=False
        )

    async def staged_ids(self) -> set[str]:
        """这台机器上已经完整分发过的数据集 id。"""
        reply = await self._channel.request("manifest", root=str(self._root))
        found: set[str] = set()
        for row in reply.get("entries", []):
            parts = str(row[0]).split("/")
            if len(parts) == 2 and parts[1] == COMPLETE_MARKER:
                found.add(parts[0])
        return found

    async def evict(self, keep: set[str]) -> tuple[str, ...]:
        """删掉不在 ``keep`` 里的数据集目录。"""
        removable = sorted(await self.staged_ids() - keep)
        if removable:
            await self._channel.request(
                "remove",
                paths=[str(self._root / dataset_id) for dataset_id in removable],
            )
        return tuple(removable)

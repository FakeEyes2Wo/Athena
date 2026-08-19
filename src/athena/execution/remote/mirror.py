"""工作区镜像：本地 worktree 为准，把增量推上去、把产物拉回来。

为什么是镜像而不是「远端为家」（设计文档 §4.6）：本地 worktree 是 git 提交的
依据（``LocalGitWorkspace.commit`` 在本地算 diff），``read_file``/``write_file``
也绑死在本地目录。镜像的改动面最小——git 提交、文件工具、agent 的心智模型全部
不变。代价写在明处：**checkpoint、特征缓存这类大文件留在远端不回来**，必须由
``pull`` 的返回值告诉上层「哪些路径只存在于远端」，否则 agent 会以为文件丢了并重跑。

增量判据是 ``(相对路径, size, sha256)`` 的集合差，**不用 mtime**：跨时区、跨文件
系统、跨传输方式的 mtime 都不可靠，而这里的一端是 Windows 笔记本。

一个具体的坑：worktree 根的 ``.git`` 是一个指回**源仓库**的纯文本指针。原样推到
远端，那边的 git 会去解析一条不存在的宿主机路径。它在默认排除名单里。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from athena.execution.remote.channel import RemoteChannel

# 永不镜像的目录/文件名。``.git`` 见模块文档；其余是纯本地缓存与虚拟环境——
# 传过去既慢又错（venv 里全是宿主机绝对路径）。
DEFAULT_EXCLUDES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ipynb_checkpoints",
        ".athena",
    }
)

# 单个文件推上去的体积上限（字节）。超过它的一律不推：工作区里出现几百 MB 的
# 文件，几乎总是本地跑出来的中间产物，重算比传快。
MAX_PUSH_BYTES = 32 * 1024 * 1024

_HASH_BLOCK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileEntry:
    """镜像清单里的一条：相对 posix 路径 + 大小 + 内容哈希。"""

    path: str
    size: int
    sha256: str


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(_HASH_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def local_manifest(
    root: Path, *, excludes: frozenset[str] = DEFAULT_EXCLUDES
) -> dict[str, FileEntry]:
    """本地一棵子树的清单，键是相对 posix 路径。"""
    entries: dict[str, FileEntry] = {}
    if not root.is_dir():
        return entries
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in excludes for part in relative.parts):
            continue
        key = relative.as_posix()
        entries[key] = FileEntry(key, path.stat().st_size, _digest(path))
    return entries


@dataclass(frozen=True, slots=True)
class PushReport:
    """一次上推的结果。"""

    uploaded: tuple[str, ...]
    deleted: tuple[str, ...]
    skipped_too_large: tuple[str, ...]
    bytes_sent: int


@dataclass(frozen=True, slots=True)
class PullReport:
    """一次回拉的结果。

    ``remote_only`` 是本设计里必须外显的那一项：它们**存在于远端但没有拉回来**。
    上层要把它写进证据，否则 agent 会以为文件丢了。
    """

    downloaded: tuple[str, ...]
    remote_only: tuple[str, ...]
    bytes_received: int


class WorkspaceMirror:
    """把一个本地工作区镜像到远端的某个目录。"""

    def __init__(
        self,
        channel: RemoteChannel,
        *,
        local_root: Path,
        remote_root: str,
        excludes: frozenset[str] = DEFAULT_EXCLUDES,
        max_push_bytes: int = MAX_PUSH_BYTES,
    ) -> None:
        self._channel = channel
        self._local = Path(local_root)
        self._remote = PurePosixPath(remote_root)
        self._excludes = excludes
        self._max_push_bytes = max_push_bytes

    @property
    def remote_root(self) -> str:
        """远端工作区根（posix 路径）。"""
        return str(self._remote)

    @property
    def local_root(self) -> Path:
        """本地工作区根。"""
        return self._local

    def local_manifest(self) -> dict[str, FileEntry]:
        """本地工作区当前的清单。"""
        return local_manifest(self._local, excludes=self._excludes)

    async def fetch(self, relative: str) -> bytes:
        """取回工作区里某个相对路径的字节。"""
        return await self._channel.read_file(self.remote_path(relative))

    def remote_path(self, relative: str) -> str:
        """把工作区相对路径映射成远端绝对路径。"""
        return str(self._remote / PurePosixPath(relative))

    async def remote_manifest(self, subdir: str = "") -> dict[str, FileEntry]:
        """远端一棵子树的清单。"""
        root = self.remote_path(subdir) if subdir else str(self._remote)
        reply = await self._channel.request(
            "manifest", root=root, exclude=sorted(self._excludes)
        )
        return {
            row[0]: FileEntry(row[0], int(row[1]), str(row[2]))
            for row in reply.get("entries", [])
        }

    async def push(self, *, prune: bool = True) -> PushReport:
        """把本地相对远端的增量推上去。"""
        await self._channel.request("mkdir", path=str(self._remote))
        local = local_manifest(self._local, excludes=self._excludes)
        remote = await self.remote_manifest()

        uploaded: list[str] = []
        oversized: list[str] = []
        sent = 0
        for key, entry in sorted(local.items()):
            existing = remote.get(key)
            if existing is not None and existing.sha256 == entry.sha256:
                continue
            if entry.size > self._max_push_bytes:
                oversized.append(key)
                continue
            data = (self._local / key).read_bytes()
            await self._channel.write_file(self.remote_path(key), data)
            uploaded.append(key)
            sent += len(data)

        deleted: list[str] = []
        if prune:
            stale = [key for key in sorted(remote) if key not in local]
            if stale:
                await self._channel.request(
                    "remove", paths=[self.remote_path(key) for key in stale]
                )
                deleted = stale
        return PushReport(tuple(uploaded), tuple(deleted), tuple(oversized), sent)

    async def pull(
        self, subdirs: tuple[str, ...], *, max_bytes: int = MAX_PUSH_BYTES
    ) -> PullReport:
        """只把声明过的产出目录拉回来，其余留在远端并如实报告。

        ``subdirs`` 就是 manifest 的 ``outputs``：预测目录与 report。刻意不做
        「把整个工作区同步回来」——那会把 checkpoint 一起拖回控制节点。
        """
        downloaded: list[str] = []
        remote_only: list[str] = []
        received = 0
        for subdir in subdirs:
            entries = await self.remote_manifest(subdir)
            for key, entry in sorted(entries.items()):
                relative = f"{subdir}/{key}" if subdir else key
                if entry.size > max_bytes:
                    remote_only.append(relative)
                    continue
                data = await self._channel.read_file(self.remote_path(relative))
                target = self._local / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                downloaded.append(relative)
                received += len(data)
        return PullReport(tuple(downloaded), tuple(remote_only), received)

"""工作区镜像：本地 worktree 为准，把增量推上去、把产物拉回来。"""

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from athena.execution.remote.channel import RemoteChannel
from athena.execution.remote.ssh import SshBackend
from athena.execution.runtime import CommandRequest, CommandResult

# 永不镜像的目录/文件名（缓存、venv 与 .git 指针）。
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

# 单文件推送上限（字节）：大文件几乎总是可重算的中间产物。
MAX_PUSH_BYTES = 32 * 1024 * 1024

_HASH_BLOCK = 1024 * 1024

PULLED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".sh",
        ".toml",
        ".cfg",
        ".ini",
        ".json",
        ".yaml",
        ".yml",
        ".md",
        ".txt",
        ".csv",
        ".log",
    }
)
PULLED_MAX_BYTES = 4 * 1024 * 1024


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
    """一次回拉的结果。"""

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

    def _remote_path(self, relative: str) -> str:
        """把工作区相对路径映射成远端绝对路径。"""
        return str(self._remote / PurePosixPath(relative))

    async def remote_manifest(self, subdir: str = "") -> dict[str, FileEntry]:
        """远端一棵子树的清单。"""
        root = self._remote_path(subdir) if subdir else str(self._remote)
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
        pending: list[str] = []
        sent = 0
        for key, entry in sorted(local.items()):
            existing = remote.get(key)
            if existing is not None and existing.sha256 == entry.sha256:
                continue
            if entry.size > self._max_push_bytes:
                oversized.append(key)
                continue
            pending.append(key)
            sent += entry.size
        if pending:
            await self._channel.send_files(
                (self._remote_path(key), self._local / key) for key in pending
            )
            uploaded = pending

        deleted: list[str] = []
        if prune:
            stale = [key for key in sorted(remote) if key not in local]
            if stale:
                await self._channel.request(
                    "remove", paths=[self._remote_path(key) for key in stale]
                )
                deleted = stale
        return PushReport(tuple(uploaded), tuple(deleted), tuple(oversized), sent)

    async def pull(
        self, subdirs: tuple[str, ...], *, max_bytes: int = MAX_PUSH_BYTES
    ) -> PullReport:
        """只把声明过的产出目录拉回来，其余留在远端并如实报告。"""
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
                data = await self._channel.read_file(self._remote_path(relative))
                target = self._local / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                downloaded.append(relative)
                received += len(data)
        return PullReport(tuple(downloaded), tuple(remote_only), received)


class MirroredBackend:
    """Execute remotely while mirroring source changes and declared outputs."""

    def __init__(self, inner: SshBackend, mirror: WorkspaceMirror) -> None:
        self._inner = inner
        self._mirror = mirror
        self._remote_only: set[str] = set()

    @property
    def name(self) -> str:
        """Return the remote host name."""
        return self._inner.name

    @property
    def inner(self) -> SshBackend:
        """Expose the executor used by lease management."""
        return self._inner

    @property
    def remote_only(self) -> tuple[str, ...]:
        """Return output paths retained only on the remote host."""
        return tuple(sorted(self._remote_only))

    def describe(self, workspace_root: str | Path) -> str:
        """Describe the machine that executes commands."""
        return self._inner.describe(workspace_root)

    def env_ref(self, name: str) -> str:
        """Format an environment reference for the remote shell."""
        return self._inner.env_ref(name)

    def ensure_environment(self) -> None:
        """Confirm the leased remote environment is ready."""
        self._inner.ensure_environment()

    async def aclose(self) -> None:
        """Close the underlying remote channel."""
        await self._inner.aclose()

    async def run(
        self,
        *,
        workspace_root: Path,
        request: CommandRequest,
    ) -> CommandResult:
        """Push source, execute remotely, then pull changed source files."""
        await self._mirror.push()
        result = await self._inner.run(
            workspace_root=workspace_root,
            request=request,
        )
        await self._pull_sources()
        return result

    async def collect_outputs(self, subdirs: tuple[str, ...]) -> PullReport:
        """Pull declared output directories and remember oversized paths."""
        report = await self._mirror.pull(subdirs)
        self._remote_only.update(report.remote_only)
        return report

    async def _pull_sources(self) -> None:
        remote = await self._mirror.remote_manifest()
        local = local_manifest(
            self._mirror._local,
            excludes=self._mirror._excludes,
        )
        for key, entry in sorted(remote.items()):
            if Path(key).suffix.lower() not in PULLED_SUFFIXES:
                continue
            if entry.size > PULLED_MAX_BYTES:
                self._remote_only.add(key)
                continue
            existing = local.get(key)
            if existing is not None and existing.sha256 == entry.sha256:
                continue
            target = self._mirror._local / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                await self._mirror._channel.read_file(self._mirror._remote_path(key))
            )

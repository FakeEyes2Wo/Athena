"""镜像后端：把「工作区在两台机器上」这件事收在一个地方。"""

from pathlib import Path

from athena.core.tool_types import EmitEvent
from athena.execution.remote.mirror import PullReport, WorkspaceMirror
from athena.execution.remote.ssh import SshBackend
from athena.execution.runtime import CommandResult

# 命令后拉回本地的后缀：只回收会进 git 提交的源码/配置/报告。
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

# 单文件回拉上限（字节）：大 csv/日志是产物，不是源码。
PULLED_MAX_BYTES = 4 * 1024 * 1024


class MirroredBackend:
    """给远程后端套上工作区镜像；实现 ``ExecutionBackend``。"""

    def __init__(
        self,
        inner: SshBackend,
        mirror: WorkspaceMirror,
        *,
        pulled_suffixes: frozenset[str] = PULLED_SUFFIXES,
        pulled_max_bytes: int = PULLED_MAX_BYTES,
    ) -> None:
        self._inner = inner
        self._mirror = mirror
        self._suffixes = pulled_suffixes
        self._max_bytes = pulled_max_bytes
        self._remote_only: set[str] = set()

    @property
    def name(self) -> str:
        """后端标识（远程主机名）。"""
        return self._inner.name

    @property
    def inner(self) -> SshBackend:
        """被包住的执行后端。"""
        return self._inner

    @property
    def mirror(self) -> WorkspaceMirror:
        """底层镜像。"""
        return self._mirror

    @property
    def remote_only(self) -> tuple[str, ...]:
        """只存在于远端、没有拉回来的路径——必须进证据。"""
        return tuple(sorted(self._remote_only))

    def describe(self, workspace_root: str | Path) -> str:
        """Runtime 块来自远端。"""
        return self._inner.describe(workspace_root)

    def env_ref(self, name: str) -> str:
        """环境变量按远端 shell 的语法引用。"""
        return self._inner.env_ref(name)

    def ensure_environment(self) -> None:
        """环境根在租约建立时已经建好。"""
        self._inner.ensure_environment()

    async def aclose(self) -> None:
        """关掉底层通道。"""
        await self._inner.aclose()

    async def run(
        self,
        *,
        command: str | None = None,
        argv: list[str] | None = None,
        workspace_root: Path,
        workdir: Path,
        timeout_s: int,
        emit: EmitEvent | None = None,
    ) -> CommandResult:
        """推增量 → 远端执行 → 拉回源码类改动。"""
        await self._mirror.push()
        result = await self._inner.run(
            command=command,
            argv=argv,
            workspace_root=workspace_root,
            workdir=workdir,
            timeout_s=timeout_s,
            emit=emit,
        )
        await self._pull_sources()
        return result

    async def collect_outputs(self, subdirs: tuple[str, ...]) -> PullReport:
        """把 manifest 声明的产出目录整个拉回来（含预测文件与 report）。"""
        report = await self._mirror.pull(subdirs)
        self._remote_only.update(report.remote_only)
        return report

    async def _pull_sources(self) -> None:
        """把远端改动过的源码类小文件同步回本地。"""
        remote = await self._mirror.remote_manifest()
        local = self._mirror.local_manifest()
        for key, entry in sorted(remote.items()):
            if Path(key).suffix.lower() not in self._suffixes:
                continue
            if entry.size > self._max_bytes:
                self._remote_only.add(key)
                continue
            existing = local.get(key)
            if existing is not None and existing.sha256 == entry.sha256:
                continue
            target = self._mirror.local_root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await self._mirror.fetch(key))

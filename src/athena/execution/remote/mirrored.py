"""镜像后端：把「工作区在两台机器上」这件事收在一个地方。

``SshBackend`` 只管把命令送过去、把输出流回来。**什么时候同步文件**是另一件事，
放在这里，理由是它的策略需要单独讲清楚，而不是散落在执行路径里。

策略（设计文档 §4.6 甲案，本地为准）：

- **每条命令之前**推增量。agent 刚用 ``write_file`` 写的脚本必须先到远端，
  否则命令跑的是上一版——而且这种错完全不报错，只是结果不对。
- **每条命令之后**只拉回「源码类小文件」加上声明过的产出目录。刻意不整棵拉：
  那会把 checkpoint、特征缓存一起拖回控制节点。
- 留在远端的东西必须**被点名**（``remote_only``）。不点名的话 agent 会以为文件
  丢了并重跑——这是镜像方案的代价里最容易伤人的一处。

为什么拉回来的是「源码类」而不是全部：``LocalGitWorkspace.commit`` 在本地算 diff，
可信修订必须包含 agent 在远端改出来的代码。二进制产物不进提交，也就不必回来。
"""

from pathlib import Path

from athena.core.tool_types import EmitEvent
from athena.execution.remote.mirror import PullReport, WorkspaceMirror
from athena.execution.remote.ssh import SshBackend
from athena.execution.runtime import CommandResult

# 命令之后会被拉回本地的文件后缀：源码、配置、报告、小体量表格。
# 判据是「进不进 git 提交」，不是「是不是文本」——.pt/.ckpt/.parquet 明确不回来。
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

# 拉回单个文件的体积上限（字节）。超过它的即便后缀符合也留在远端：
# 一个 500 MB 的 csv 是产物，不是源码。
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

    # 直接转发的部分：镜像不改变「这台机器是什么样」。
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
        """把 manifest 声明的产出目录整个拉回来（含预测文件与 report）。

        这里不按后缀过滤：产出是被声明过的东西，本来就该回到本地被打分。
        评估器永远在控制节点上跑——远端只产出 ``predictions/``。
        """
        report = await self._mirror.pull(subdirs)
        self._remote_only.update(report.remote_only)
        return report

    async def _pull_sources(self) -> None:
        """把远端改动过的源码类小文件同步回本地。

        agent 常常直接用 ``shell_command`` 生成/修改脚本（``sed``、重定向、
        代码生成器）。这些改动必须回到本地 worktree，否则可信修订提交的是
        一份和真正跑过的东西不一样的代码。
        """
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

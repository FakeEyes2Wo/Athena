"""执行后端：一台机器上的「跑命令」能力。

``ExecutionRuntime`` 之上的全部代码——Supervisor、PlanRunner、agent 的
``shell_command``——都不知道命令跑在哪台机器上。这条协议就是那道界线：
**界线之上完全看不到远程，界线之下完全彻底。**

界线画在这里而不是别处，理由见
``docs/architecture/2026-08-19-remote-gpu-execution-design.md`` 第三节。简述：

- 给 agent 一个 ssh 工具最省事，但取消、计费、复现全部失守，而且模型有能力
  ssh 到持有测试标签的那台机器。
- 只把 manifest 的 argv 远程化、``shell_command`` 留在本地也不行：agent 的循环是
  「写脚本 → 跑 → 读报错 → 改」，它会先用 ``shell_command`` 试跑、看到
  ``CUDA not available``，然后写出 CPU 代码。**调试回路和执行回路必须同机。**

因此后端的粒度是「一台机器 + 一份环境」，由上层按 Plan 绑定，绑定后该 Plan 的
每一条命令都落在同一台机器、同一个目录。

``describe`` 单独列进协议而不是留在本地实现里，是因为它守着一条不变式：
**模型看到的环境描述必须来自真正执行命令的那台机器。** 今天 ``os_name`` /
``shell_parts`` 都读本地 ``os.name``；远端是 Linux 而 summary 说 powershell 时，
模型会老老实实给 bash 写 PowerShell。
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from athena.core.contracts import ArtifactStore
from athena.core.tool_types import EmitEvent
from athena.execution.runtime import (
    CommandExecutor,
    CommandResult,
    EnvironmentManager,
)


@runtime_checkable
class ExecutionBackend(Protocol):
    """一台机器上的执行能力。"""

    @property
    def name(self) -> str:
        """后端标识：本地为 ``local``，远程为主机别名。

        它要进实验证据（``placement.host``）。异构算力下，A 臂拿 A100、B 臂拿
        3090 会让墙钟受限实验的分数不可比；不记下来就无从发现。
        """

    def describe(self, workspace_root: str | Path) -> str:
        """注入 system prompt 的 Runtime 块——必须描述**这台**机器。"""

    def env_ref(self, name: str) -> str:
        """按这台机器的 shell 语法引用一个环境变量。"""

    def ensure_environment(self) -> None:
        """确保这台机器上的环境根可用（幂等）。"""

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
        """执行一条命令并流式推送事件。"""

    async def collect_outputs(self, subdirs: tuple[str, ...]) -> None:
        """把 manifest 声明的产出目录取到本地，供**本地**的可信评估器打分。

        本地后端无事可做（文件本来就在那儿）。远程后端必须真的拉回来——
        评估器与测试标签永远留在控制节点，远端只产出 ``predictions/``。
        """

    async def aclose(self) -> None:
        """释放后端持有的资源（本地无事可做；远程要关掉常驻通道）。"""


class LocalBackend:
    """在控制节点自己身上执行——今天的全部行为，一字未改。

    它把既有的 ``EnvironmentManager``（探测/环境构造/描述）与 ``CommandExecutor``
    （起进程/流式/超时/杀进程树）包成协议的形状。P1 的验收判据就是这一条：
    ``test/unit/execution/`` 全部原样通过。
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        environment_root: str | Path,
        data_root: str | Path | None = None,
        store: ArtifactStore | None = None,
    ) -> None:
        self._environment = EnvironmentManager(
            project_root=project_root,
            environment_root=environment_root,
            data_root=data_root,
        )
        self._store = store

    @property
    def name(self) -> str:
        """本地后端的标识。"""
        return "local"

    @property
    def environment(self) -> EnvironmentManager:
        """底层环境管理器（``ExecutionRuntime`` 的既有能力经它转发）。"""
        return self._environment

    def describe(self, workspace_root: str | Path) -> str:
        """本机的 Runtime 块。"""
        return self._environment.runtime_summary(Path(workspace_root))

    def env_ref(self, name: str) -> str:
        """按本机 shell 的语法引用环境变量。"""
        return self._environment.env_ref(name)

    def ensure_environment(self) -> None:
        """补齐环境根的 pyproject.toml。"""
        self._environment.ensure_project()

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
        """在本机执行；``argv`` 不经 shell，``command`` 走本机 shell。"""
        if argv is not None:
            shell, shell_args = None, None
        else:
            shell, shell_args = self._environment.shell_parts()
        executor = CommandExecutor(
            env=self._environment.build_env(workspace_root),
            persist=self._store.put_text if self._store is not None else None,
        )
        return await executor.run(
            command=command,
            argv=argv,
            workdir=workdir,
            shell=shell,
            shell_args=shell_args,
            timeout_s=timeout_s,
            emit=emit,
        )

    async def collect_outputs(self, subdirs: tuple[str, ...]) -> None:
        """本地执行，产出本来就在工作区里。"""
        del subdirs

    async def aclose(self) -> None:
        """本地后端没有需要释放的东西。"""
        return None

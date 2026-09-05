"""执行后端：一台机器上的「跑命令」能力。"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from athena.core.contracts import ArtifactStore
from athena.execution.runtime import (
    CommandExecutor,
    CommandRequest,
    CommandResult,
    EnvironmentManager,
)


@runtime_checkable
class ExecutionBackend(Protocol):
    """一台机器上的执行能力。"""

    @property
    def name(self) -> str:
        """后端标识：本地为 ``local``，远程为主机别名。"""

    def describe(self, workspace_root: str | Path) -> str:
        """注入 system prompt 的 Runtime 块——必须描述**这台**机器。"""

    def env_ref(self, name: str) -> str:
        """按这台机器的 shell 语法引用一个环境变量。"""

    def ensure_environment(self) -> None:
        """确保这台机器上的环境根可用（幂等）。"""

    async def run(
        self,
        *,
        workspace_root: Path,
        request: CommandRequest,
    ) -> CommandResult:
        """执行一条命令并流式推送事件。"""

    async def collect_outputs(self, subdirs: tuple[str, ...]) -> None:
        """把 manifest 声明的产出目录取到本地，供**本地**的可信评估器打分。"""

    async def aclose(self) -> None:
        """释放后端持有的资源（本地无事可做；远程要关掉常驻通道）。"""


class LocalBackend:
    """在控制节点自己身上执行——今天的全部行为，一字未改。"""

    def __init__(
        self,
        *,
        environment_root: str | Path,
        data_root: str | Path | None = None,
        store: ArtifactStore | None = None,
    ) -> None:
        self._environment = EnvironmentManager(
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
        workspace_root: Path,
        request: CommandRequest,
    ) -> CommandResult:
        """在本机执行；参数列表不经 shell，字符串命令走本机 shell。"""
        if isinstance(request.command, list):
            shell, shell_args = None, None
        else:
            shell, shell_args = self._environment.shell_parts()
        executor = CommandExecutor(
            env=self._environment.build_env(
                workspace_root,
                predict_features=request.predict_features,
                evaluation_split=request.evaluation_split,
            ),
            persist=self._store.put_text if self._store is not None else None,
        )
        return await executor.run(
            command=request.command,
            workdir=(
                Path(request.workdir) if request.workdir is not None else workspace_root
            ),
            shell=shell,
            shell_args=shell_args,
            timeout_s=request.timeout_s,
            emit=request.emit,
        )

    async def collect_outputs(self, subdirs: tuple[str, ...]) -> None:
        """本地执行，产出本来就在工作区里。"""

    async def aclose(self) -> None:
        """本地后端没有需要释放的东西。"""

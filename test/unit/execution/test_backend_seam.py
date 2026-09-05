"""执行后端这道缝：``ExecutionRuntime`` 之上不得有任何东西假设命令跑在本机。

这一组用例守的是设计文档第五节的前两条不变式：

1. 一个 Plan 的所有命令都经同一个后端走掉——没有任何旁路。
2. 模型看到的环境描述来自**真正执行命令的那台机器**，不是本地 ``os.name``。

第 2 条是最容易悄悄坏掉的：``runtime_summary`` 只要重新读一次本地 ``os.name``，
本地跑起来一切正常，接上远程后模型才开始给 bash 写 PowerShell——而且这种错的
表现是「命令语法错误」，指不到真正的原因。
"""

from pathlib import Path

import pytest

from athena.execution import ExecutionBackend, LocalBackend
from athena.execution.runtime import (
    CommandRequest,
    CommandResult,
    ExecutionContext,
    ExecutionRuntime,
)


class _FakeBackend:
    """假装自己是另一台机器的后端；记下每一次调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    @property
    def name(self) -> str:
        return "gpu-01"

    def describe(self, workspace_root) -> str:
        return f"Runtime:\n- OS: Linux\n- Shell: bash\n- Workspace: {workspace_root}"

    def env_ref(self, name: str) -> str:
        return f"${name}"

    def ensure_environment(self) -> None:
        self.calls.append({"op": "ensure_environment"})

    async def run(
        self,
        *,
        workspace_root: Path,
        request: CommandRequest,
    ) -> CommandResult:
        self.calls.append(
            {
                "op": "run",
                "command": request.command,
                "workspace_root": workspace_root,
                "workdir": (
                    Path(request.workdir)
                    if request.workdir is not None
                    else workspace_root
                ),
                "timeout_s": request.timeout_s,
                "predict_features": request.predict_features,
            }
        )
        return CommandResult(ok=True, stdout="", stderr="", exit_code=0)

    async def aclose(self) -> None:
        self.closed = True


def _runtime(tmp_path: Path, backend) -> ExecutionRuntime:
    return ExecutionRuntime(
        project_root=tmp_path, environment_root=tmp_path, backend=backend
    )


def test_the_local_backend_satisfies_the_protocol(tmp_path) -> None:
    backend = LocalBackend(environment_root=tmp_path)
    assert isinstance(backend, ExecutionBackend)
    assert backend.name == "local"


def test_the_default_backend_is_local(tmp_path) -> None:
    """不给后端时必须是本地——远程绝不能靠默认值悄悄生效，反过来也一样。"""
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    assert runtime.backend.name == "local"


def test_the_runtime_block_comes_from_the_executing_machine(tmp_path) -> None:
    """描述必须出自后端。本机是 Windows，后端说自己是 Linux，模型就该看到 Linux。"""
    runtime = _runtime(tmp_path, _FakeBackend())
    summary = runtime.runtime_summary(tmp_path)

    assert "- OS: Linux" in summary
    assert "Shell: bash" in summary
    assert "powershell" not in summary.lower()
    assert runtime.env_ref("ATHENA_ENV_ROOT") == "$ATHENA_ENV_ROOT"


@pytest.mark.asyncio
async def test_every_command_goes_through_the_backend(tmp_path) -> None:
    """shell 字符串与 argv 两条路径都不得绕过后端。"""
    backend = _FakeBackend()
    runtime = _runtime(tmp_path, backend)
    context = ExecutionContext(
        project_root=tmp_path, workspace_root=tmp_path, environment_root=tmp_path
    )

    await runtime.run(context, CommandRequest(command="echo hi", timeout_s=11))
    await runtime.run(
        context, CommandRequest(command=["python", "train.py"], timeout_s=22)
    )

    runs = [call for call in backend.calls if call["op"] == "run"]
    assert [call["command"] for call in runs] == [
        "echo hi",
        ["python", "train.py"],
    ]
    assert [call["timeout_s"] for call in runs] == [11, 22]


@pytest.mark.asyncio
async def test_workdir_defaults_to_the_workspace_and_is_passed_through(
    tmp_path,
) -> None:
    """workdir 与 workspace_root 都要到后端手里：远程侧要靠它们定位目录。"""
    backend = _FakeBackend()
    runtime = _runtime(tmp_path, backend)
    workspace = tmp_path / "ws"
    context = ExecutionContext(
        project_root=tmp_path, workspace_root=workspace, environment_root=tmp_path
    )

    await runtime.run(context, CommandRequest(command="pwd", timeout_s=5))
    await runtime.run(
        context,
        CommandRequest(command="pwd", timeout_s=5, workdir=workspace / "sub"),
    )

    runs = [call for call in backend.calls if call["op"] == "run"]
    assert all(call["workspace_root"] == workspace for call in runs)
    assert [call["workdir"] for call in runs] == [workspace, workspace / "sub"]


@pytest.mark.asyncio
async def test_closing_the_runtime_closes_the_backend(tmp_path) -> None:
    """远程后端持着一条常驻 ssh 通道；关不掉就等于把它漏在那里。"""
    backend = _FakeBackend()
    await _runtime(tmp_path, backend).aclose()
    assert backend.closed is True


def test_ensure_environment_is_delegated(tmp_path) -> None:
    backend = _FakeBackend()
    _runtime(tmp_path, backend).ensure_environment()
    assert {"op": "ensure_environment"} in backend.calls

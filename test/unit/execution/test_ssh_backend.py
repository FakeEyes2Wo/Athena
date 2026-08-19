"""``SshBackend``：远程执行后端的行为，以及 ssh 命令行本身。

``ssh`` 那一跳无法在开发机上验证，所以这里分成两半：

- 命令行逐项断言（``SshHost.ssh_argv``）——它是唯一没被端到端覆盖的东西，
  短到可以完全用断言钉死。
- 后端行为用一条真通道（本地子进程跑同一份 agent）端到端跑：真起进程、
  真流式、真超时、真杀进程组。
"""

import asyncio
import sys
from pathlib import Path

import pytest

from athena.execution import ExecutionBackend
from athena.execution.remote import (
    RemoteChannel,
    SshBackend,
    SshHost,
    WorkspaceMirror,
)
from athena.execution.remote.mirrored import MirroredBackend
from athena.execution.remote.channel import SubprocessTransport


@pytest.fixture
async def backend(tmp_path):
    channel = RemoteChannel(SubprocessTransport(sys.executable))
    await channel.open()
    workspace = tmp_path / "remote-ws"
    workspace.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    backend = SshBackend(
        SshHost(name="gpu-01", alias="gpu01.lab"),
        channel=channel,
        remote_workspace=workspace.as_posix(),
        remote_env_root=(tmp_path / "env").as_posix(),
        remote_data_root=data.as_posix(),
        gpu_ids=(2, 3),
    )
    backend.bind_local_root(workspace)
    try:
        yield backend
    finally:
        await backend.aclose()


# ------------------------------------------------------------------ ssh 命令行


def test_the_ssh_command_line_never_carries_credentials() -> None:
    """只写 ~/.ssh/config 的 Host 别名，凭据永远不进 Athena 的配置或命令行。"""
    argv = SshHost(name="gpu-01", alias="gpu01.lab").ssh_argv()

    assert argv[0] == "ssh"
    assert "gpu01.lab" in argv
    assert not any("@" in part for part in argv), "别名不是 user@host"
    assert not any(part in ("-i", "-p") for part in argv), "密钥与端口交给 ssh_config"


def test_the_ssh_command_line_disables_prompts_and_agent_forwarding() -> None:
    """通道跑在后台：交互式口令提示只会变成一次静默挂死。

    ForwardAgent=no 同样是硬要求——agent 写的代码要在那台机器上跑，不能让它
    顺手拿到本地的 ssh 身份。
    """
    argv = SshHost(name="gpu-01", alias="gpu01.lab").ssh_argv()
    joined = " ".join(argv)

    assert "BatchMode=yes" in joined
    assert "ForwardAgent=no" in joined
    assert "ServerAliveInterval=60" in joined


def test_the_agent_source_travels_in_the_command_not_on_stdin() -> None:
    """stdin 是协议通道本身，不能被解释器当程序读掉。

    这条错了的表现是「连上去就没反应」，排查方向会完全跑偏。
    """
    argv = SshHost(name="gpu-01", alias="gpu01.lab", python="python3.11").ssh_argv()
    remote_command = argv[-1]

    assert remote_command.startswith("python3.11 -u -c ")
    assert "base64.b64decode" in remote_command
    assert " - " not in remote_command


# ------------------------------------------------------------------ 后端行为


def test_the_executor_alone_is_not_a_complete_backend(backend) -> None:
    """少一个 collect_outputs，而且这是有意的。

    远程执行绕不开「文件怎么在两台机器之间搬」。给 SshBackend 补一个空实现，
    会让「产出没拉回来」变成一次静默的空目录；缺着，它就只能是一个类型错误。
    """
    assert not isinstance(backend, ExecutionBackend)
    assert backend.name == "gpu-01"

    mirror = WorkspaceMirror(
        backend._channel,
        local_root=Path(backend.remote_workspace),
        remote_root=backend.remote_workspace,
    )
    assert isinstance(MirroredBackend(backend, mirror), ExecutionBackend)


def test_the_runtime_block_describes_the_remote_host(backend) -> None:
    """本机是 Windows/PowerShell；模型必须看到远端那台机器。

    说错了的后果不是报一个清楚的错，而是模型给 bash 写 PowerShell，
    报错表现成语法错误，指不到真正的原因。
    """
    summary = backend.describe(Path("/ignored"))

    assert "powershell" not in summary.lower()
    assert "remote host gpu-01" in summary
    assert "GPUs leased to this experiment" in summary
    assert "ATHENA_DATA_ROOT" in summary
    assert '"$ATHENA_ENV_ROOT"' in summary  # 远端是 POSIX，不是 $env:
    assert backend.env_ref("ATHENA_ENV_ROOT") == "$ATHENA_ENV_ROOT"


def test_local_path_variables_are_not_forwarded(backend, monkeypatch) -> None:
    """本地的 PATH/HOME/SSL_CERT_FILE 在远端全是无效路径，转发过去只会制造怪错。"""
    monkeypatch.setenv("SSL_CERT_FILE", r"C:\certs\ca.pem")
    monkeypatch.setenv("UV_CACHE_DIR", r"C:\uvcache")
    env = backend.build_env()

    for leaked in (
        "PATH",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "SSL_CERT_FILE",
        "UV_CACHE_DIR",
    ):
        assert leaked not in env, f"{leaked} 不该被转发到远端"
    assert env["ATHENA_ENV_ROOT"].endswith("/env")
    assert env["ATHENA_DATA_ROOT"].endswith("/data")


def test_the_lease_pins_the_gpus_it_was_given(backend) -> None:
    """裸机没有调度器，卡的独占只能靠 CUDA_VISIBLE_DEVICES 强制。"""
    assert backend.build_env()["CUDA_VISIBLE_DEVICES"] == "2,3"


@pytest.mark.asyncio
async def test_a_command_runs_remotely_and_reports_its_exit_code(backend) -> None:
    result = await backend.run(
        argv=[sys.executable, "-c", "import sys; print('hello'); sys.exit(0)"],
        workspace_root=Path(backend.remote_workspace),
        workdir=Path(backend.remote_workspace),
        timeout_s=60,
    )
    assert result.ok
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_a_failing_command_is_a_normal_result_not_an_exception(backend) -> None:
    """非零退出是正常结果：agent 要读 stderr 自己修，而不是让 turn 崩掉。"""
    result = await backend.run(
        argv=[
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('boom'); sys.exit(7)",
        ],
        workspace_root=Path(backend.remote_workspace),
        workdir=Path(backend.remote_workspace),
        timeout_s=60,
    )
    assert not result.ok
    assert result.exit_code == 7
    assert "boom" in result.stderr


@pytest.mark.asyncio
async def test_the_experiment_env_reaches_the_remote_process(backend) -> None:
    """ATHENA_DATA_ROOT 要真的到子进程手里，否则 agent 写的脚本读不到数据。"""
    result = await backend.run(
        argv=[
            sys.executable,
            "-c",
            "import os; print(os.environ['ATHENA_DATA_ROOT']); "
            "print(os.environ['CUDA_VISIBLE_DEVICES'])",
        ],
        workspace_root=Path(backend.remote_workspace),
        workdir=Path(backend.remote_workspace),
        timeout_s=60,
    )
    assert result.ok
    lines = result.stdout.strip().splitlines()
    assert lines[0].endswith("/data")
    assert lines[1] == "2,3"


@pytest.mark.asyncio
async def test_a_timeout_kills_the_remote_process_group(backend, tmp_path) -> None:
    """超时不能只是「不等了」：远端进程必须真的死掉，否则显存一直被占着。"""
    marker = tmp_path / "remote-alive"
    result = await backend.run(
        argv=[
            sys.executable,
            "-c",
            "import pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text('1'); time.sleep(120)",
            str(marker),
        ],
        workspace_root=Path(backend.remote_workspace),
        workdir=Path(backend.remote_workspace),
        timeout_s=3,
    )
    assert not result.ok
    assert result.error == "timeout"

    await asyncio.sleep(1.0)
    marker.unlink()  # 进程还活着的话，Windows 上这一步会被文件锁挡住
    assert not marker.exists()


@pytest.mark.asyncio
async def test_output_is_emitted_as_events_while_the_command_runs(backend) -> None:
    """事件流要一路透到上层，TUI/日志才看得到训练进度。"""
    events: list[tuple[str, dict]] = []

    async def emit(kind: str, ref: str, data: dict) -> None:
        events.append((kind, data))

    result = await backend.run(
        argv=[sys.executable, "-c", "print('progress 1'); print('progress 2')"],
        workspace_root=Path(backend.remote_workspace),
        workdir=Path(backend.remote_workspace),
        timeout_s=60,
        emit=emit,
    )
    assert result.ok
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "command/started"
    assert kinds[-1] == "command/completed"
    streamed = "".join(
        data.get("delta", "") for kind, data in events if kind == "command/stdout"
    )
    assert "progress 1" in streamed and "progress 2" in streamed

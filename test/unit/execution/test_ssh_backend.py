"""``SshBackend``：远程执行后端的行为，以及 ssh 命令行本身。

``ssh`` 那一跳无法在开发机上验证，所以这里分成两半：

- 命令行逐项断言（``SshHost.ssh_argv``）——它是唯一没被端到端覆盖的东西，
  短到可以完全用断言钉死。
- 后端行为用一条真通道（本地子进程跑同一份 agent）端到端跑：真起进程、
  真流式、真超时、真杀进程组。
"""

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.execution import ExecutionBackend
from athena.execution.remote import (
    RemoteChannel,
    SshBackend,
    SshHost,
    WorkspaceMirror,
)
from athena.execution.remote.mirrored import MirroredBackend
from athena.execution.remote.channel import SubprocessTransport
from athena.execution.remote.ssh import REMOTE_COMMAND_LIMIT_BYTES


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
        remote_data_root=data.as_posix(),
        gpu_ids=(2, 3),
    )
    backend.bind_local_root(workspace)
    try:
        yield backend
    finally:
        await backend.aclose()


# ssh 命令行


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


def test_the_remote_command_stays_under_the_silent_truncation_limit() -> None:
    """远端命令必须短——超长不会报错，会被**静默截断**。

    真机量到：Win32-OpenSSH 9.5 在 8189 字节处砍断远端命令，退出码仍是 0，
    远端 bash 只抱怨引号没配对，看起来像转义写错了而不是"太长了"。把 agent
    源码 base64 进命令行（约 18 KiB）在 Linux 控制节点上能过，从 Windows 上必断。
    """
    argv = SshHost(name="gpu-01", alias="gpu01.lab", python="python3.11").ssh_argv()
    remote_command = argv[-1]

    assert remote_command.startswith("python3.11 -u -c ")
    assert len(remote_command.encode("utf-8")) <= REMOTE_COMMAND_LIMIT_BYTES
    assert " - " not in remote_command, "绝不能是 python3 -：那会把协议通道读掉"


def test_an_oversized_remote_command_fails_here_not_silently_over_there() -> None:
    """越界要在本地当场红。

    静默截断的排查成本极高：远端只会给一句风马牛不相及的语法错误。
    """
    host = SshHost(
        name="gpu-01", alias="gpu01.lab", python="p" * REMOTE_COMMAND_LIMIT_BYTES
    )
    with pytest.raises(ValueError, match="truncates it silently"):
        host.ssh_argv()


# 后端行为


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
    assert '"$ATHENA_DATA_ROOT"' in summary  # 远端是 POSIX，不是 $env:
    assert backend.env_ref("ATHENA_DATA_ROOT") == "$ATHENA_DATA_ROOT"
    assert "- Python: missing" not in summary, "事实随握手一起到；不该退化成缺省值"


def test_the_runtime_block_says_the_interpreter_is_fixed(backend) -> None:
    """远端只用现成的解释器——这一条必须**说给 agent 听**，而不是让它撞。

    原本无条件教的 ``uv add --project "$ATHENA_ENV_ROOT"`` 有两处错：真机上第一台
    机器就没有 uv，而那个变量指向的还是一个我们凭空建的空目录。
    """
    backend._channel.ready = {
        **backend.facts,
        "packages": {"torch": "2.5.1+cu124", "numpy": "2.1.3"},
    }
    summary = backend.describe(Path("/ignored"))

    assert "used as-is" in summary
    assert "- Installed: torch 2.5.1+cu124, numpy 2.1.3" in summary
    assert "cannot install packages" in summary
    assert "uv add" not in summary and "pip install" not in summary


def test_local_path_variables_are_not_forwarded(backend, monkeypatch) -> None:
    """本地的 HOME/SSL_CERT_FILE 在远端全是无效路径，转发过去只会制造怪错。"""
    monkeypatch.setenv("SSL_CERT_FILE", r"C:\certs\ca.pem")
    monkeypatch.setenv("UV_CACHE_DIR", r"C:\uvcache")
    env = backend.build_env()

    for leaked in ("HOME", "USERPROFILE", "TEMP", "SSL_CERT_FILE", "UV_CACHE_DIR"):
        assert leaked not in env, f"{leaked} 不该被转发到远端"
    assert env["ATHENA_DATA_ROOT"].endswith("/data")
    # 远端没有 Athena 管的环境；给一个指向空目录的变量只会诱使 agent 去 uv add。
    assert "ATHENA_ENV_ROOT" not in env


def test_the_remote_path_comes_from_the_remote() -> None:
    """PATH 必须问远端要，而且是远端自己的那一份。

    真机上量到的：非交互式 ssh **不读** ``~/.bashrc``，容器里 conda 那一段 PATH
    因此不在，``ssh host python3 -V`` 直接 command not found——同一台机器交互
    登录却一切正常。远端 agent 在 probe 里把自己解释器的 bin 目录顶到最前报上来，
    控制节点据此拼 PATH；少了这一步，agent 写的第一条 ``python train.py`` 就死。
    """
    # 钉一份真实的 Linux 事实：断言的是拼装规则，不是跑测试这台机器长什么样。
    backend = SshBackend(
        SshHost(name="gpu-01", alias="gpu01.lab"),
        channel=SimpleNamespace(
            ready={"path": "/root/miniconda3/bin:/usr/local/bin:/usr/bin:/bin"}
        ),
        remote_workspace="/scratch/athena/leases/h1/workspace",
    )

    assert backend.build_env()["PATH"] == (
        "/root/miniconda3/bin:/usr/local/bin:/usr/bin:/bin"
    ), "远端的 PATH 原样用；解释器所在目录由远端顶到最前"


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


@pytest.mark.asyncio
async def test_a_long_remote_log_keeps_its_tail_and_its_full_copy(
    tmp_path, monkeypatch
) -> None:
    """远端长日志：模型看到头尾，证据 artifact 里是**完整**的那份。

    这条是本地模拟里长期没被看住的一处：远程后端原本只累积头部，
    ``output_ref`` 于是存了一份被砍过的文本——却仍然对外声称是完整输出。
    结果是 agent 拿着 ref 去查 traceback，查到的还是没有 traceback 的那份，
    而训练日志的 traceback 恰恰只在尾巴上。
    """
    monkeypatch.setattr("athena.execution.runtime.MAX_OUTPUT_CHARS", 300)
    store = LocalArtifactStore(tmp_path / "artifacts")
    channel = RemoteChannel(SubprocessTransport(sys.executable))
    await channel.open()
    workspace = tmp_path / "remote-ws"
    workspace.mkdir()
    backend = SshBackend(
        SshHost(name="gpu-01", alias="gpu01.lab"),
        channel=channel,
        remote_workspace=workspace.as_posix(),
        store=store,
    )
    backend.bind_local_root(workspace)

    try:
        result = await backend.run(
            argv=[
                sys.executable,
                "-c",
                "print('HEAD-MARK'); print('n' * 5000); print('TAIL-MARK')",
            ],
            workspace_root=workspace,
            workdir=workspace,
            timeout_s=60,
        )
    finally:
        await backend.aclose()

    assert result.truncated and result.output_ref is not None
    assert "HEAD-MARK" in result.stdout, "头部要留：它说明跑的是什么"
    assert "TAIL-MARK" in result.stdout, "尾部要留：失败现场在最后"
    assert "chars elided" in result.stdout, "省略必须写在正文里"

    full = await store.get_text(result.output_ref)
    assert "HEAD-MARK" in full and "TAIL-MARK" in full
    assert full.count("n") >= 5000, "artifact 必须是完整输出，不是被砍过的那份"


async def test_a_backend_without_a_bound_local_root_still_runs(tmp_path) -> None:
    """没调过 ``bind_local_root`` 也要能跑。

    ``compute --check`` 就是这么用的：它建一个后端只为渲染 Runtime 块，从不绑本地
    根。折算不出远端路径时落回工作区根，而不是在一个从未出现在 ``__init__`` 里的
    属性上抛 AttributeError——那种错读代码时完全看不出来。
    """
    channel = RemoteChannel(SubprocessTransport(sys.executable))
    await channel.open()
    workspace = tmp_path / "remote-ws"
    workspace.mkdir()
    backend = SshBackend(
        SshHost(name="gpu-01", alias="gpu01.lab"),
        channel=channel,
        remote_workspace=workspace.as_posix(),
    )
    try:
        assert "Runtime:" in backend.describe(tmp_path)
        result = await backend.run(
            argv=[sys.executable, "-c", "print('ok')"],
            workspace_root=tmp_path,
            workdir=tmp_path / "somewhere-else",
            timeout_s=30,
        )
        assert result.ok and result.stdout.strip() == "ok"
    finally:
        await backend.aclose()

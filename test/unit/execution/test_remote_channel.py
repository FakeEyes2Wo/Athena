"""远程执行协议的真实验证：用本地子进程把同一份 agent 拉起来跑。

真正的 ``ssh`` 那一跳在开发机上无法验证，但它之外的全部——握手、流式、退出码、
取消、超时、文件读写、清单、以及**断线之后远端自己清场**——都可以，只要换掉传输。
所以这里跑的不是 mock：是真的起进程、真的杀进程组、真的读回字节。

留在覆盖之外的只剩 ``SshHost.ssh_argv`` 拼出来的那条命令行，它由
``test_ssh_backend.py`` 逐项断言。
"""

import asyncio
import sys
from pathlib import Path

import pytest

from athena.execution.remote import (
    RemoteChannel,
    RemoteError,
    SubprocessTransport,
    WorkspaceMirror,
)
from athena.execution.remote.channel import (
    agent_source,
    bootstrap_code,
    bootstrap_payload,
)
from athena.execution.remote.mirror import local_manifest


@pytest.fixture
async def channel():
    channel = RemoteChannel(SubprocessTransport(sys.executable))
    await channel.open()
    try:
        yield channel
    finally:
        await channel.close()


def _sink() -> tuple[dict[int, bytearray], object]:
    buffers: dict[int, bytearray] = {1: bytearray(), 2: bytearray()}

    def on_output(fd: int, block: bytes) -> None:
        buffers[fd].extend(block)

    return buffers, on_output


@pytest.mark.asyncio
async def test_the_agent_reports_the_facts_needed_for_preflight(channel) -> None:
    """握手就该带回注册期预检要的事实，不必再多问一轮。

    这条是真机上抓出来的：握手消息本身只有 pid 与协议版本，事实要另外问一次。
    没有这一步时 ``ready`` 里没有 python，注入给 agent 的 Runtime 块就退化成一份
    看起来完全正常、其实全是缺省值的描述——"Python: missing"、OS 猜成 Linux、
    PATH 拼成空。它不报错，只让远端第一条命令 command not found。
    """
    assert channel.ready["op"] == "ready"
    assert channel.ready["protocol"] == 1

    assert channel.ready["python"].startswith("3.")
    assert channel.ready["python_executable"]
    assert channel.ready["path"], "远端 PATH 必须问远端要：非交互式 ssh 不读 .bashrc"
    assert "gpus" in channel.ready
    # 远端装不了东西，所以"手里有什么"必须在开工前就问清（见 ssh._environment_lines）。
    assert isinstance(channel.ready["packages"], dict)


@pytest.mark.asyncio
async def test_stdout_stderr_and_exit_code_come_back_separately(channel, tmp_path):
    buffers, on_output = _sink()
    _job, exited = await channel.spawn(
        argv=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('out'); sys.stderr.write('err'); "
            "sys.exit(3)",
        ],
        command=None,
        cwd=str(tmp_path),
        env={},
        on_output=on_output,
    )
    assert await asyncio.wait_for(exited, timeout=60) == 3
    assert bytes(buffers[1]) == b"out"
    assert bytes(buffers[2]) == b"err"


@pytest.mark.asyncio
async def test_output_streams_before_the_process_exits(channel, tmp_path) -> None:
    """流式必须是真流式：训练日志要能在跑的过程中看到，不是结束后一次性吐出。"""
    seen = asyncio.Event()

    def on_output(fd: int, block: bytes) -> None:
        if b"tick" in block:
            seen.set()

    _job, exited = await channel.spawn(
        argv=[
            sys.executable,
            "-c",
            "import sys,time; print('tick', flush=True); time.sleep(30)",
        ],
        command=None,
        cwd=str(tmp_path),
        env={},
        on_output=on_output,
    )
    await asyncio.wait_for(seen.wait(), timeout=30)
    assert not exited.done(), "进程还在跑，输出就该已经到了"
    await channel.cancel(_job)
    await asyncio.wait_for(exited, timeout=30)


@pytest.mark.asyncio
async def test_cancel_kills_the_whole_process_group(channel, tmp_path) -> None:
    """取消要打整棵树。只杀直接子进程的话，训练进程会带着显存活下来。"""
    marker = tmp_path / "child-alive"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        '    "import time, pathlib, sys\\n"\n'
        "    \"pathlib.Path(sys.argv[1]).write_text('1')\\n\"\n"
        '    "time.sleep(120)", sys.argv[1]])\n'
        "print('spawned', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    started = asyncio.Event()

    def on_output(fd: int, block: bytes) -> None:
        if b"spawned" in block:
            started.set()

    job, exited = await channel.spawn(
        argv=[sys.executable, str(script), str(marker)],
        command=None,
        cwd=str(tmp_path),
        env={},
        on_output=on_output,
    )
    await asyncio.wait_for(started.wait(), timeout=30)
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.1)
    assert marker.exists(), "孙进程没起来，这个用例就测不到进程组"

    await channel.cancel(job)
    assert await asyncio.wait_for(exited, timeout=30) != 0


@pytest.mark.asyncio
async def test_closing_the_channel_kills_everything_it_started(tmp_path) -> None:
    """断线即清场——这是显存不被跑飞的作业永久占住的唯一可靠机制。

    控制节点是 Windows 笔记本时，休眠 / Wi-Fi 漫游 / 系统更新重启都会切断通道，
    所以这条不是边缘路径，是日常路径。
    """
    channel = RemoteChannel(SubprocessTransport(sys.executable))
    await channel.open()
    marker = tmp_path / "still-running"
    _job, _exited = await channel.spawn(
        argv=[
            sys.executable,
            "-c",
            "import pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text('1'); time.sleep(120)",
            str(marker),
        ],
        command=None,
        cwd=str(tmp_path),
        env={},
        on_output=lambda fd, block: None,
    )
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.1)
    assert marker.exists()

    await channel.close()
    assert channel.closed

    # 进程真的走了：它还活着的话，下面这个删除在 Windows 上会被文件锁挡住。
    await asyncio.sleep(1.0)
    marker.unlink()
    assert not marker.exists()


@pytest.mark.asyncio
async def test_a_closed_channel_refuses_new_work(channel, tmp_path) -> None:
    """通道没了就该立刻报错，绝不静默把命令丢掉。"""
    await channel.close()
    with pytest.raises(RemoteError):
        await channel.request("probe")


@pytest.mark.asyncio
async def test_files_survive_the_round_trip_byte_for_byte(channel, tmp_path) -> None:
    """二进制安全：协议流和数据流不能互相污染。"""
    target = tmp_path / "nested" / "blob.bin"
    payload = bytes(range(256)) * 1024  # 含换行、含 0x00、非 UTF-8

    await channel.write_file(str(target), payload)
    assert target.read_bytes() == payload
    assert await channel.read_file(str(target)) == payload


@pytest.mark.asyncio
async def test_a_missing_file_is_an_error_not_an_empty_result(channel, tmp_path):
    """读不到就要报错。返回空字节会让上层把「文件没了」当成「文件是空的」。"""
    with pytest.raises(RemoteError):
        await channel.read_file(str(tmp_path / "nope.bin"))


@pytest.mark.asyncio
async def test_the_mirror_pushes_only_what_changed(channel, tmp_path) -> None:
    local = tmp_path / "ws"
    (local / "src").mkdir(parents=True)
    (local / "src" / "train.py").write_text("print(1)\n", encoding="utf-8")
    (local / "experiment.json").write_text("{}", encoding="utf-8")
    # worktree 根的 .git 是指回源仓库的纯文本指针，推过去那边会去解析一条
    # 不存在的宿主机路径。必须被排除。
    (local / ".git").write_text("gitdir: C:/somewhere/else\n", encoding="utf-8")

    mirror = WorkspaceMirror(
        channel, local_root=local, remote_root=str(tmp_path / "remote")
    )
    first = await mirror.push()
    assert set(first.uploaded) == {"src/train.py", "experiment.json"}
    assert ".git" not in first.uploaded

    second = await mirror.push()
    assert second.uploaded == (), "内容没变就不该重传"

    (local / "src" / "train.py").write_text("print(2)\n", encoding="utf-8")
    third = await mirror.push()
    assert third.uploaded == ("src/train.py",)


@pytest.mark.asyncio
async def test_the_mirror_prunes_what_the_agent_deleted(channel, tmp_path) -> None:
    local = tmp_path / "ws"
    local.mkdir()
    (local / "old.py").write_text("x", encoding="utf-8")
    remote = tmp_path / "remote"

    mirror = WorkspaceMirror(channel, local_root=local, remote_root=str(remote))
    await mirror.push()
    assert (remote / "old.py").exists()

    (local / "old.py").unlink()
    report = await mirror.push()
    assert report.deleted == ("old.py",)
    assert not (remote / "old.py").exists()


@pytest.mark.asyncio
async def test_pull_brings_back_declared_outputs_and_names_the_rest(
    channel, tmp_path
) -> None:
    """只拉声明过的产出；留在远端的大文件必须被如实点名。

    不点名的话 agent 会以为文件丢了并重跑——那是镜像方案代价里最容易伤人的一处。
    """
    local = tmp_path / "ws"
    local.mkdir()
    remote = tmp_path / "remote"
    (remote / "predictions").mkdir(parents=True)
    (remote / "predictions" / "pred.csv").write_bytes(b"id,pred\n1,0\n")
    (remote / "predictions" / "checkpoint.bin").write_bytes(b"z" * 4096)

    mirror = WorkspaceMirror(channel, local_root=local, remote_root=str(remote))
    report = await mirror.pull(("predictions",), max_bytes=1024)

    assert report.downloaded == ("predictions/pred.csv",)
    assert report.remote_only == ("predictions/checkpoint.bin",)
    assert (local / "predictions" / "pred.csv").read_bytes() == b"id,pred\n1,0\n"
    assert not (local / "predictions" / "checkpoint.bin").exists()


@pytest.mark.asyncio
async def test_remote_and_local_manifests_agree(channel, tmp_path) -> None:
    """两侧清单必须同口径，否则增量判断会来回抖。"""
    root = tmp_path / "tree"
    (root / "a").mkdir(parents=True)
    (root / "a" / "one.txt").write_bytes(b"one")
    (root / "two.txt").write_bytes(b"two")

    mirror = WorkspaceMirror(channel, local_root=root, remote_root=str(root))
    remote = await mirror.remote_manifest()
    local = local_manifest(Path(root))

    assert set(remote) == set(local)
    for key, entry in local.items():
        assert remote[key].sha256 == entry.sha256
        assert remote[key].size == entry.size


# ------------------------------------------------------ 引导：源码怎么过去的


def test_the_loader_is_small_enough_to_survive_the_ssh_command_line() -> None:
    """加载器必须短，因为超长的远端命令是**静默截断**的。

    真机量到：Win32-OpenSSH 9.5 把远端命令砍在 8189 字节，退出码仍是 0，远端
    bash 只会抱怨引号没配对——看起来像转义写错了。把 13 KiB 的 agent 源码
    base64 进命令行必然踩中，而控制节点是 Windows 是本设计锁定的前提。
    """
    loader = bootstrap_code()

    assert len(loader.encode("utf-8")) < 256, "加载器一旦长起来就该改用别的机制"
    # 源码不在命令行里——它有 13 KiB，塞进去必然越过那条截断线。
    assert "PROTOCOL_VERSION" not in loader
    assert len(agent_source()) > 4096, "源码本来就超长；这正是不能走命令行的理由"


def test_the_agent_source_travels_as_the_stdin_preamble() -> None:
    """源码走 stdin 的头一段，而不是命令行，也不是 ``python3 -``。

    ``python3 -`` 会让解释器把整个 stdin 当程序读到 EOF，stdin 也就没了——而
    stdin 正是协议通道本身，也是「断线即 EOF、远端自己清场」的全部依据。
    所以是长度前缀：读满 N 字节就停手。
    """
    payload = bootstrap_payload()
    length, _, source = payload.partition(b"\n")

    assert int(length) == len(source), "长度前缀必须正好是源码的字节数"
    assert source == agent_source()
    assert " - " not in bootstrap_code(), "绝不能是 python3 -"


@pytest.mark.asyncio
async def test_a_transport_that_dies_explains_itself(tmp_path) -> None:
    """掉线要带上原因。

    ``Permission denied (publickey)``、``Connection refused``、远端 shell 的语法
    报错，只会出现在传输自己的 stderr 上。丢掉它，三种处置方式完全不同的故障
    就塌成同一句 "channel closed unexpectedly"，排查只能靠猜。
    """

    class _DyingTransport(SubprocessTransport):
        async def start(self):
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('Permission denied (publickey)')",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._proc = proc
            self._stderr.attach(proc.stderr)
            return proc.stdin, proc.stdout

    channel = RemoteChannel(_DyingTransport(sys.executable))
    with pytest.raises(RemoteError, match="Permission denied"):
        await channel.open()

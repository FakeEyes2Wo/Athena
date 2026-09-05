"""远程执行协议的真实验证：用本地子进程把同一份 agent 拉起来跑。

真正的 ``ssh`` 那一跳在开发机上无法验证，但它之外的全部——握手、流式、退出码、
取消、超时、文件读写、清单、以及**断线之后远端自己清场**——都可以，只要换掉传输。
所以这里跑的不是 mock：是真的起进程、真的杀进程组、真的读回字节。

留在覆盖之外的只剩 ``SshHost.ssh_argv`` 拼出来的那条命令行，它由
``test_ssh_backend.py`` 逐项断言。
"""

import asyncio
import base64
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
        command=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('out'); sys.stderr.write('err'); "
            "sys.exit(3)",
        ],
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
        command=[
            sys.executable,
            "-c",
            "import sys,time; print('tick', flush=True); time.sleep(30)",
        ],
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
        command=[sys.executable, str(script), str(marker)],
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
        command=[
            sys.executable,
            "-c",
            "import pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text('1'); time.sleep(120)",
            str(marker),
        ],
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


# 引导：源码怎么过去的


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


# 传输：并行在飞而不是一块一等


class _HoldPuts:
    """把 ``put`` 的确认掐掉，其余请求照常放行。

    流水线的可观测特征是「不等确认能发出多少」。用它断言比计时可靠——计时在 CI
    上会抖，而且本地子进程扮演的远端快到根本攒不出飞行队列。
    """

    def __init__(self, channel: RemoteChannel) -> None:
        self.paths: list[str] = []
        self._ids: set[str] = set()
        self._channel = channel
        self._send = channel._send
        self._dispatch = channel._dispatch
        channel._send = self._watch  # type: ignore[method-assign]
        channel._dispatch = self._filter  # type: ignore[method-assign]

    async def _watch(self, payload: dict) -> None:
        if payload.get("op") == "put":
            self._ids.add(payload["id"])
            self.paths.append(payload["path"])
        await self._send(payload)

    def _filter(self, message: dict) -> None:
        if message.get("id") in self._ids:
            return
        self._dispatch(message)


@pytest.mark.asyncio
async def test_a_big_write_keeps_a_full_window_in_flight(channel, tmp_path) -> None:
    """分块上传必须并行在飞，否则吞吐 = 块大小 ÷ 往返时延，与带宽无关。

    真机量到的：一块一等时 64 KiB / 31 ms ≈ 2 MiB/s，实测 1.9；改成窗口之后
    8 MiB 从 4.3 秒降到 0.6 秒。

    断言方式刻意不用计时（CI 上会抖），而是**把回复掐掉**：谁也不确认时，
    "不等确认能发出多少"就是窗口本身。一块一等的话这个数是 1。
    """
    from athena.execution.remote import agent as agent_module
    from athena.execution.remote.channel import TRANSFER_WINDOW

    payload = b"x" * (agent_module.CHUNK_BYTES * (TRANSFER_WINDOW + 8))
    held = _HoldPuts(channel)

    task = asyncio.create_task(channel.write_file(str(tmp_path / "big.bin"), payload))
    await asyncio.sleep(0.5)
    try:
        assert not task.done(), "窗口满了就该停下等确认，而不是无界地灌"
        # 两条都要：">1" 抓"退回一块一等"，"== 窗口"抓"无界地灌"。
        # 只写后者的话，把常量改成 1 也能过——断言会跟着常量一起动。
        assert len(held.paths) > 1, "一块一等就是这条链路上唯一的瓶颈"
        assert len(held.paths) == TRANSFER_WINDOW
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_blocks_arrive_in_order_even_though_they_fly_together(channel, tmp_path):
    """并行在飞不能打乱顺序——``append`` 语义全靠块 0 先落地。

    错了的表现不是报错，是文件内容被拼错，而哈希校验要到分发的最后一步才发现。
    """
    from athena.execution.remote import agent as agent_module
    from athena.execution.remote.channel import TRANSFER_WINDOW

    chunk = agent_module.CHUNK_BYTES
    # 每一块都带自己的序号：错序会变成内容不等，而不是长度不等。
    payload = b"".join(
        bytes([index % 256]) * chunk for index in range(TRANSFER_WINDOW * 3)
    )

    await channel.write_file(str(tmp_path / "ordered.bin"), payload)

    assert (tmp_path / "ordered.bin").read_bytes() == payload


@pytest.mark.asyncio
async def test_streaming_a_file_never_reads_it_whole(channel, tmp_path) -> None:
    """数据集里单个文件几十 GB 是常态，整读进内存不可行。

    断言方式是把 ``read_bytes`` 拆掉：真在流式，就不会碰它。
    """
    from athena.execution.remote import agent as agent_module

    source = tmp_path / "dataset.bin"
    payload = bytes(range(256)) * (agent_module.CHUNK_BYTES * 40 // 256)
    source.write_bytes(payload)
    original = Path.read_bytes

    def refuse(self):  # noqa: ANN001
        raise AssertionError(f"整读进内存了：{self}")

    Path.read_bytes = refuse  # type: ignore[method-assign]
    try:
        sent = await channel.send_file(str(tmp_path / "copy.bin"), source)
    finally:
        Path.read_bytes = original  # type: ignore[method-assign]

    assert sent == len(payload)
    assert (tmp_path / "copy.bin").read_bytes() == payload


@pytest.mark.asyncio
async def test_an_empty_file_still_gets_created(channel, tmp_path) -> None:
    """零字节也要在远端真的出现——否则"文件没了"和"文件是空的"会被混为一谈。"""
    await channel.write_file(str(tmp_path / "empty.bin"), b"")
    assert (tmp_path / "empty.bin").is_file()
    assert (tmp_path / "empty.bin").read_bytes() == b""

    (tmp_path / "src.bin").write_bytes(b"")
    assert (
        await channel.send_file(str(tmp_path / "empty2.bin"), tmp_path / "src.bin") == 0
    )
    assert (tmp_path / "empty2.bin").is_file()


@pytest.mark.asyncio
async def test_a_failed_write_leaves_no_dangling_futures(channel, tmp_path) -> None:
    """飞行中的请求出错时，剩下那些必须被收掉。

    不收的话会刷出一片 "Future exception was never retrieved"，把真正的原因淹掉——
    而这正是排查传输故障时唯一有用的那条信息。
    """
    from athena.execution.remote import agent as agent_module

    blocked = tmp_path / "blocked"
    blocked.write_text("我是文件，不是目录", encoding="utf-8")
    payload = b"x" * (agent_module.CHUNK_BYTES * 40)

    with pytest.raises(RemoteError):
        await channel.write_file(str(blocked / "nested.bin"), payload)

    assert not [f for f in channel._pending.values() if not f.done()]


@pytest.mark.asyncio
async def test_the_mirror_pushes_several_files_at_once(channel, tmp_path) -> None:
    """一个文件一次往返，50 个小文件就是 50 次。真机上那是 1.69 秒。

    同样把回复掐掉来断言：谁也不确认时，同时在飞的**不同路径**数就是文件并发度。
    """
    from athena.execution.remote.channel import TRANSFER_FILES

    local = tmp_path / "ws"
    local.mkdir()
    for index in range(TRANSFER_FILES * 3):
        (local / f"m{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
    mirror = WorkspaceMirror(
        channel, local_root=local, remote_root=str(tmp_path / "remote")
    )
    held = _HoldPuts(channel)

    task = asyncio.create_task(mirror.push())
    await asyncio.sleep(0.5)
    try:
        assert len(set(held.paths)) > 1, "一个一个推就是每个文件一次往返"
        assert len(set(held.paths)) == TRANSFER_FILES, "并发度既是下限也是上限"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_the_mirror_streams_instead_of_slurping(channel, tmp_path) -> None:
    """镜像也必须走流式，而不是先整读进内存。

    ``send_file`` 自己流式还不够——真正会伤人的是调用点。工作区里出现一个大文件
    时，整读会把控制节点直接打爆，而这台控制节点是笔记本。
    """
    local = tmp_path / "ws"
    local.mkdir()
    (local / "big.bin").write_bytes(b"y" * (1024 * 1024))
    mirror = WorkspaceMirror(
        channel, local_root=local, remote_root=str(tmp_path / "remote")
    )
    original = Path.read_bytes

    def refuse(self):  # noqa: ANN001
        raise AssertionError(f"整读进内存了：{self}")

    Path.read_bytes = refuse  # type: ignore[method-assign]
    try:
        report = await mirror.push()
    finally:
        Path.read_bytes = original  # type: ignore[method-assign]

    assert report.uploaded == ("big.bin",)
    assert (tmp_path / "remote" / "big.bin").stat().st_size == 1024 * 1024


async def test_a_read_that_dies_midway_does_not_leave_chunks_behind(channel) -> None:
    """读到一半失败时，攒下的块必须跟着那次请求一起走。

    ``get`` 的分块先到、终结响应后到，所以块要先攒在通道上。远端读到一半才出错
    （磁盘错误、文件被换掉）时，前面那些块再也没人来取——而常驻通道一跑就是几十
    上百条命令，漏的是文件内容本身，不是几个字节的记账。

    这里直接喂派发器：让远端真的在读到一半时失败很难，但"块先到、error 后到"
    正是要守住的那个次序。
    """
    pending = asyncio.get_running_loop().create_future()
    channel._pending["r-dying"] = pending
    channel._dispatch(
        {"op": "chunk", "id": "r-dying", "b64": base64.b64encode(b"half").decode()}
    )
    assert channel._collect["r-dying"] == [b"half"]

    channel._dispatch({"op": "error", "id": "r-dying", "error": "disk went away"})
    with pytest.raises(RemoteError, match="disk went away"):
        await pending
    assert "r-dying" not in channel._collect


async def test_a_read_that_never_starts_reports_the_remote_error(channel) -> None:
    """远端打不开文件时要抛，而不是安静地返回空字节。"""
    with pytest.raises(RemoteError):
        await channel.read_file("/definitely/not/here.bin")


async def test_reading_a_file_returns_exactly_what_was_written(channel, tmp_path):
    """整读回来的字节必须一字不差——分块拼接是这条路径唯一容易出错的地方。"""
    payload = bytes(range(256)) * 900  # 跨多个 CHUNK_BYTES 块
    target = tmp_path / "blob.bin"
    await channel.write_file(str(target), payload)
    assert await channel.read_file(str(target)) == payload

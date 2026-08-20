"""控制节点这一半：一条常驻通道，一行一条 JSON。

``RemoteTransport`` 把「怎么连上去」和「连上之后说什么」分开，理由不是抽象洁癖：
真正的 ``ssh`` 那一跳无法在开发机上验证，而协议、流式、取消、EOF 清理、镜像增量
全都可以——只要把同一份 ``agent.py`` 用本地子进程拉起来。于是有两个实现：

- ``SubprocessTransport``：本地起同一份 agent，测试用，**协议全覆盖**。
- ``SshTransport``（见 ``ssh.py``）：``ssh <host> python3 -c <加载器>``，只多了一跳。

两者拉起 agent 的方式**必须是同一套**（同一个加载器、同一段 stdin 前导），否则
本地那几百条用例覆盖的就不是真机跑的那条路径。留在 ssh 那一跳之外未被覆盖的，
只剩下 ssh 命令行本身——它短到可以一眼看完。
"""

import asyncio
import base64
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from athena.execution.remote import agent as agent_module

# 等远端 agent 报 ready 的上限（秒）。SSH 握手 + 解释器启动都算在里面；
# 超时通常意味着连不上或者远端没有 python3，两者都该立刻失败而不是挂着。
HANDSHAKE_TIMEOUT_S = 60

# 单行上限（字节）。asyncio 的 StreamReader 默认只给 64 KiB，而协议是一行一条
# JSON：一块 64 KiB 的输出经 base64 就是约 88 KiB，正好越界，表现为通道"莫名其妙
# 断开"——排查方向会完全跑偏。清单消息（一棵树的全部条目）也可能很长。
# 这里给足，并在读循环里把越界单独报出来，而不是让它伪装成掉线。
LINE_LIMIT_BYTES = 16 * 1024 * 1024

# 传输窗口：一次可以有多少块 / 多少个文件在飞而不等确认。
#
# **这是量出来的。** 分块上传原本一块一等，于是吞吐 = 块大小 ÷ 往返时延，与带宽
# 无关：64 KiB ÷ 31 ms ≈ 2 MiB/s，实测 1.9 MiB/s；同一条链路裸 ``scp`` 是
# 5.9 MiB/s。窗口把上限抬到 ``窗口 × 块 ÷ RTT``，16 × 64 KiB 在 31 ms 的链路上
# 足够跑到 32 MiB/s，早已越过链路本身的上限。
#
# 上界也是有意的：飞行中的字节数 = 窗口 × 块（base64 后约 ×1.33）。无界地灌会把
# 内存和管道缓冲一起吃掉，而且失去背压——``drain`` 正是靠管道满了才挡一下。
TRANSFER_WINDOW = 16

# 同时推几个文件。50 个小文件原本要 50 次往返（实测 1.69 s，34 ms 一个，正好一个
# RTT）。文件之间互不相干，并发是安全的；这里给的是并发上限，不是并发度。
TRANSFER_FILES = 4


class RemoteError(RuntimeError):
    """远端把一个请求判成失败。"""


class RemoteTransport(Protocol):
    """一条能把 agent 源码喂进去、之后双向说话的字节管道。"""

    @property
    def description(self) -> str:
        """给人看的连接描述（进日志与错误信息）。"""

    async def start(self) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
        """建立连接，返回 ``(写入端, 读取端)``。"""

    async def stop(self) -> None:
        """断开连接。**必须真的让远端的 stdin EOF**，否则孤儿进程收不掉。"""

    def diagnostics(self) -> str:
        """连接自己的错误输出（ssh 的 stderr 之类），用于解释一次掉线。

        没有这一条，``Permission denied (publickey)``、``Connection refused``、
        远端 shell 的语法报错会全部塌成同一句"channel closed unexpectedly"，
        而它们的处置方式完全不同。
        """
        return ""


# 保留多少 stderr 用于解释掉线。够放下 ssh 的几行报错和一个 Python traceback。
DIAGNOSTIC_TAIL_BYTES = 4096


class StderrTail:
    """后台把一条 stderr 抽干，只留最后一段。

    必须抽：管道写满之后远端会阻塞在写 stderr 上，表现成"连上了但没反应"。
    只留最后一段：这是给人看的诊断，不是日志。
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._task: asyncio.Task[None] | None = None

    def attach(self, reader: asyncio.StreamReader | None) -> None:
        """挂上一条流，开始后台抽取。"""
        if reader is None:
            return
        self._task = asyncio.create_task(self._drain(reader))

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                block = await reader.read(4096)
                if not block:
                    return
                self._buffer.extend(block)
                if len(self._buffer) > DIAGNOSTIC_TAIL_BYTES * 2:
                    del self._buffer[:-DIAGNOSTIC_TAIL_BYTES]
        except (asyncio.CancelledError, ConnectionError, OSError, ValueError):
            return

    def stop(self) -> None:
        """停止抽取；已经收到的那一段留着。"""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def text(self) -> str:
        """最后一段 stderr，已折成单行。"""
        tail = bytes(self._buffer[-DIAGNOSTIC_TAIL_BYTES:])
        return " ".join(tail.decode("utf-8", "replace").split())


class SubprocessTransport:
    """本地子进程跑 agent；测试与「控制节点即计算节点」都用它。"""

    def __init__(self, python: str) -> None:
        self._python = python
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr = StderrTail()

    @property
    def description(self) -> str:
        return f"subprocess:{self._python}"

    def diagnostics(self) -> str:
        return self._stderr.text()

    async def start(self) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
        self._proc = await asyncio.create_subprocess_exec(
            self._python,
            "-u",
            "-c",
            bootstrap_code(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT_BYTES,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._stderr.attach(self._proc.stderr)
        return self._proc.stdin, self._proc.stdout

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        self._stderr.stop()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()


def agent_source() -> bytes:
    """远端 agent 的源码。"""
    return Path(agent_module.__file__).read_bytes()


def bootstrap_code() -> str:
    """交给 ``python3 -c`` 的**第零级**加载器：读一个长度，再读那么多字节并执行。

    源码本身不走命令行，走 stdin 的头一段。这不是洁癖，是一条真机量出来的硬限制：

    > Win32-OpenSSH 9.5 把远端命令**静默截断在 8189 字节**——退出码仍是 0，
    > 远端 bash 只会抱怨引号没配对。把 13 KiB 的 agent 源码 base64 塞进命令行
    > （约 18 KiB）在 Linux 控制节点上能过，从 Windows 上必然断，而断法完全
    > 不像"太长了"。控制节点是 Windows 是本设计锁定的前提，所以这条必须绕开。

    绕法是把源码挪到 stdin，但**不能用 ``python3 -``**：那会让解释器把整个 stdin
    当程序读到 EOF，stdin 也就没了——而 stdin 正是协议通道本身，同时还是
    「连接一断即 EOF、远端自己清场」这条机制的全部依据。所以是长度前缀：
    读满 N 字节就停手，剩下的原样留在同一个 ``BufferedReader`` 里给 agent 接着用。

    副作用是好的：远端 ``ps`` 里现在是一行看得懂的加载器，而不是 18 KiB 的
    base64 糊。
    """
    return (
        "import sys;n=int(sys.stdin.buffer.readline());exec(sys.stdin.buffer.read(n))"
    )


def bootstrap_payload() -> bytes:
    """紧跟在连接建立之后写进去的那一段：``长度\\n`` + agent 源码。

    长度用十进制单独一行，因为加载器只有 ``readline`` 可用——此刻还没有任何协议。
    """
    source = agent_source()
    return f"{len(source)}\n".encode("ascii") + source


class RemoteChannel:
    """一条常驻通道上的请求/响应与流式事件分发。"""

    def __init__(self, transport: RemoteTransport) -> None:
        self._transport = transport
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._pump: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._collect: dict[str, list[bytes]] = {}
        self._streams: dict[str, Callable[[int, bytes], Any]] = {}
        self._exits: dict[str, asyncio.Future[int]] = {}
        self._counter = 0
        self._closed = False
        self.ready: dict[str, Any] = {}
        self._write_lock = asyncio.Lock()

    @property
    def description(self) -> str:
        """连接描述（进错误信息）。"""
        return self._transport.description

    @property
    def closed(self) -> bool:
        """通道是否已经关闭或掉线。"""
        return self._closed

    async def open(self) -> dict[str, Any]:
        """连上、喂源码、等 ``ready``；握手不成立即失败。"""
        self._writer, self._reader = await self._transport.start()
        # 源码是通道上的头一段字节，之后同一条流才变成协议（见 bootstrap_code）。
        self._writer.write(bootstrap_payload())
        await self._writer.drain()
        # ready 的 future 必须在读循环起来之前挂好，否则握手消息先到就被丢掉。
        ready = asyncio.get_running_loop().create_future()
        self._pending["@ready"] = ready
        self._pump = asyncio.create_task(self._read_loop())
        try:
            self.ready = await asyncio.wait_for(ready, timeout=HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.close()
            raise RemoteError(
                f"remote agent did not report ready on {self.description}"
            ) from None
        except RemoteError:
            await self.close()
            raise
        if self.ready.get("op") == "fatal":
            await self.close()
            raise RemoteError(
                f"remote agent refused to start on {self.description}: "
                f"{self.ready.get('error')}"
            )
        # 握手本身只说得出 pid 和协议版本。事实（os/shell/python/PATH/uv/GPU）要问一次
        # ——而且必须在这里问，不能指望每个调用方记得。
        #
        # 真机上量到的代价：不问的时候 ``ready`` 里没有 python，于是注入给 agent 的
        # Runtime 块显示 "Python: missing"、OS 退回默认值 "Linux"、PATH 拼成空——
        # 一份看起来完全正常、其实全是缺省值的运行时描述。那种错法不会报错，
        # 只会让远端第一条命令 command not found，而信息指不到原因。
        facts = await self.request("probe")
        self.ready = {**self.ready, **facts, "op": "ready"}
        return self.ready

    async def close(self) -> None:
        """关闭通道。远端 stdin 因此 EOF，它会杀掉自己起过的所有进程组。"""
        if self._closed:
            return
        self._closed = True
        writer = self._writer
        self._writer = None
        if writer is not None:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
                writer.close()
            except (OSError, RuntimeError):
                pass
        await self._transport.stop()
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_pending(RemoteError("remote channel closed"))

    # ---------------------------------------------------------------- 请求

    async def request(self, op: str, **fields: Any) -> dict:
        """发一条请求并等它的终结响应（``ok`` / 该 op 自己的结果 / ``error``）。"""
        future = await self.send(op, **fields)
        return await future

    async def send(self, op: str, **fields: Any) -> asyncio.Future[dict]:
        """发一条请求，**不等**响应，返回它的 future。

        分块传输靠它把多块同时放在飞行中。顺序不会因此乱：``_send`` 串行写，
        远端 agent 的读循环也是顺序派发的，所以同一个文件的块到达次序与发出
        次序一致——``append`` 语义因此仍然成立。
        """
        request_id = self._next_id()
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"op": op, "id": request_id, **fields})
        return future

    async def spawn(
        self,
        *,
        argv: list[str] | None,
        command: str | None,
        cwd: str,
        env: dict[str, str],
        on_output: Callable[[int, bytes], Any],
        shell: str | None = None,
    ) -> tuple[str, asyncio.Future[int]]:
        """起一条远端命令；返回 ``(job_id, 退出码 future)``，输出经回调流式给出。"""
        job_id = self._next_id()
        loop = asyncio.get_running_loop()
        started: asyncio.Future[dict] = loop.create_future()
        exited: asyncio.Future[int] = loop.create_future()
        self._pending[job_id] = started
        self._streams[job_id] = on_output
        self._exits[job_id] = exited
        payload: dict[str, Any] = {
            "op": "spawn",
            "id": job_id,
            "cwd": cwd,
            "env": env,
        }
        if argv is not None:
            payload["argv"] = argv
        if command is not None:
            payload["command"] = command
        if shell is not None:
            payload["shell"] = shell
        await self._send(payload)
        await started
        return job_id, exited

    async def cancel(self, job_id: str) -> None:
        """杀掉一条远端命令连同它的整个进程组。"""
        await self.request("cancel", target=job_id)

    async def read_file(self, path: str) -> bytes:
        """把远端的一个文件整读回来。"""
        request_id = self._next_id()
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._collect[request_id] = []
        await self._send({"op": "get", "id": request_id, "path": path})
        await future
        return b"".join(self._collect.pop(request_id, []))

    async def _write_blocks(
        self,
        path: str,
        blocks: AsyncIterator[bytes],
        *,
        executable: bool = False,
    ) -> int:
        """把一串块按窗口推到远端一个路径，返回写入的字节数。

        **一块一等曾是这条链路上唯一的瓶颈。** 真机量到：64 KiB 的块、31 ms 的
        往返，实测 1.9 MiB/s，而 64 KiB ÷ 31 ms ≈ 2 MiB/s——时间几乎全花在等
        确认上，跟带宽无关；同一条链路裸 ``scp`` 能到 5.9 MiB/s。窗口把上限提到
        ``窗口 × 块 ÷ RTT``。

        顺序仍然成立（见 ``send``），所以 ``append`` 语义不受影响：块 0 截断，
        其余追加。
        """
        inflight: deque[asyncio.Future[dict]] = deque()
        sent = 0
        index = 0
        try:
            async for block in blocks:
                inflight.append(
                    await self.send(
                        "put",
                        path=path,
                        b64=base64.b64encode(block).decode("ascii"),
                        append=index > 0,
                    )
                )
                sent += len(block)
                index += 1
                # 窗口满了才收一块：飞行中的字节数因此有上界。
                if len(inflight) >= TRANSFER_WINDOW:
                    await inflight.popleft()
            if index == 0 or executable:
                # 空文件也要有一次 put（否则远端根本不会出现这个文件）；
                # executable 只能在最后一块之后落，此时才知道哪一块是最后一块。
                inflight.append(
                    await self.send(
                        "put",
                        path=path,
                        b64="",
                        append=index > 0,
                        executable=executable,
                    )
                )
            while inflight:
                await inflight.popleft()
        except BaseException:
            # 剩下的 future 要收掉，但**不能等**：本次传输被取消时没人会再来回复
            # 它们，等下去就是死等。取消既不会挂，也不会刷出一片
            # "Future exception was never retrieved" 把真正的原因淹掉。
            for future in inflight:
                future.cancel()
            raise
        return sent

    async def write_file(
        self, path: str, data: bytes, *, executable: bool = False
    ) -> None:
        """把内存里的字节写到远端一个路径。"""
        limit = agent_module.CHUNK_BYTES

        async def blocks() -> AsyncIterator[bytes]:
            for start in range(0, len(data), limit):
                yield data[start : start + limit]

        await self._write_blocks(path, blocks(), executable=executable)

    async def send_file(
        self, path: str, source: Path, *, executable: bool = False
    ) -> int:
        """把本地一个文件**流式**写到远端，返回字节数。

        不整读进内存：数据集里单个文件几十 GB 是常态，而 ``write_file`` 的
        ``bytes`` 参数意味着它必须先完整装进内存。读盘放到线程里做，不挡事件循环。
        """
        limit = agent_module.CHUNK_BYTES

        async def blocks() -> AsyncIterator[bytes]:
            handle = await asyncio.to_thread(open, source, "rb")
            try:
                while True:
                    block = await asyncio.to_thread(handle.read, limit)
                    if not block:
                        return
                    yield block
            finally:
                await asyncio.to_thread(handle.close)

        return await self._write_blocks(path, blocks(), executable=executable)

    # ---------------------------------------------------------------- 内部

    def _next_id(self) -> str:
        self._counter += 1
        return f"r{self._counter}"

    async def _send(self, payload: dict) -> None:
        if self._closed or self._writer is None:
            raise RemoteError(f"remote channel is closed ({self.description})")
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            self._writer.write(line)
            await self._writer.drain()

    def _resolve(self, key: str, payload: dict) -> None:
        future = self._pending.pop(key, None)
        if future is not None and not future.done():
            future.set_result(payload)

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for exit_future in list(self._exits.values()):
            if not exit_future.done():
                exit_future.set_exception(error)
        self._exits.clear()
        self._streams.clear()

    async def _read_loop(self) -> None:
        reader = self._reader
        assert reader is not None
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError as exc:
                    # 单行越界：把原因说清楚，别让它看起来像掉线。
                    self._fail_pending(
                        RemoteError(f"remote line exceeded the buffer limit: {exc}")
                    )
                    break
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                self._dispatch(message)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._closed = True
            self._fail_pending(RemoteError(self._disconnect_reason()))

    def _disconnect_reason(self) -> str:
        """掉线的说法要带上原因。

        ``Permission denied (publickey)``、``Connection refused``、远端 shell 的
        语法报错，全都只会出现在传输自己的 stderr 上。丢掉它，这三种完全不同的
        故障就塌成同一句话，排查只能靠猜。
        """
        reason = f"remote channel closed unexpectedly ({self.description})"
        detail = ""
        try:
            detail = self._transport.diagnostics()
        except Exception:  # 诊断本身不该再抛
            detail = ""
        return f"{reason}: {detail}" if detail else reason

    def _dispatch(self, message: dict) -> None:
        op = message.get("op")
        message_id = message.get("id")
        if op in ("ready", "fatal"):
            # fatal 也走 ready 的 future：握手要么成功要么带着原因立刻失败，
            # 不能让调用方在超时上等满一分钟才知道远端根本不是 POSIX。
            self._resolve("@ready", message)
            return
        if op == "out":
            callback = self._streams.get(message_id)
            if callback is not None:
                callback(int(message.get("fd", 1)), base64.b64decode(message["b64"]))
            return
        if op == "chunk":
            self._collect.setdefault(message_id, []).append(
                base64.b64decode(message["b64"])
            )
            return
        if op == "exit":
            self._streams.pop(message_id, None)
            future = self._exits.pop(message_id, None)
            if future is not None and not future.done():
                future.set_result(int(message.get("code", -1)))
            return
        if op == "error":
            future = self._pending.pop(message_id, None)
            if future is not None and not future.done():
                future.set_exception(RemoteError(message.get("error", "remote error")))
            return
        self._resolve(message_id, message)

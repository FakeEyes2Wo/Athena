"""控制节点这一半：一条常驻通道，一行一条 JSON。"""

import asyncio
import base64
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from athena.execution.remote import agent as agent_module

# 握手超时（秒）：连不上或远端没有 python3 都该立刻失败而不是挂着。
HANDSHAKE_TIMEOUT_S = 60

# 单行上限（字节）：64 KiB 输出经 base64 约 88 KiB，会撑破 StreamReader 默认 64 KiB。
LINE_LIMIT_BYTES = 16 * 1024 * 1024

# 传输窗口：实测一块一等吞吐 = 块大小 ÷ RTT（约 2 MiB/s）；窗口 × 块 ÷ RTT 才能跑满带宽。
TRANSFER_WINDOW = 16

# 同时推几个文件：小文件不并发就是每个一次 RTT。
TRANSFER_FILES = 4

# 保留多少 stderr 用于解释掉线。
DIAGNOSTIC_TAIL_BYTES = 4096


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
        """断开连接。"""

    def diagnostics(self) -> str:
        """连接自己的错误输出（ssh 的 stderr 之类），用于解释一次掉线。"""
        return ""


class StderrTail:
    """后台把一条 stderr 抽干，只留最后一段。"""

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
        """连接描述：本地跑的是哪个解释器。"""
        return f"subprocess:{self._python}"

    def diagnostics(self) -> str:
        """子进程自己的 stderr——远端 agent 起不来时原因只在这里。"""
        return self._stderr.text()

    async def start(self) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
        """起本地子进程，返回它的 ``(stdin, stdout)``。"""
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
        """杀掉子进程并等它收尸。"""
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
    """交给 ``python3 -c`` 的**第零级**加载器：读一个长度，再读那么多字节并执行。"""
    return (
        "import sys;n=int(sys.stdin.buffer.readline());exec(sys.stdin.buffer.read(n))"
    )


def bootstrap_payload() -> bytes:
    """紧跟在连接建立之后写进去的那一段：``长度\\n`` + agent 源码。"""
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
        self._writer.write(bootstrap_payload())
        await self._writer.drain()
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
        # 必须在这里补一次 probe：事实缺失会让 Runtime 块充满缺省值，错法不报错。
        facts = await self.request("probe")
        self.ready = {**self.ready, **facts, "op": "ready"}
        return self.ready

    async def close(self) -> None:
        """关闭通道。"""
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

    async def request(self, op: str, **fields: Any) -> dict:
        """发一条请求并等它的终结响应（``ok`` / 该 op 自己的结果 / ``error``）。"""
        future = await self.send(op, **fields)
        return await future

    async def send(self, op: str, **fields: Any) -> asyncio.Future[dict]:
        """发一条请求，**不等**响应，返回它的 future。"""
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
        reply = await self.request("get", path=path)
        return b"".join(self._collect.pop(reply.get("id"), []))

    async def _write_blocks(
        self,
        path: str,
        blocks: AsyncIterator[bytes],
        *,
        executable: bool = False,
    ) -> int:
        """把一串块按窗口推到远端一个路径，返回写入的字节数。"""
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
                if len(inflight) >= TRANSFER_WINDOW:
                    await inflight.popleft()
            if index == 0 or executable:
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
            # 取消时不能等剩余 future 的回复，cancel 掉并继续抛出。
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
            """把内存里的字节切成远端一次收得下的块。"""
            for start in range(0, len(data), limit):
                yield data[start : start + limit]

        await self._write_blocks(path, blocks(), executable=executable)

    async def send_file(
        self, path: str, source: Path, *, executable: bool = False
    ) -> int:
        """把本地一个文件**流式**写到远端，返回字节数。"""
        limit = agent_module.CHUNK_BYTES

        async def blocks() -> AsyncIterator[bytes]:
            """一块一块地读文件；读盘放线程里，不挡事件循环。"""
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

    async def send_files(self, pairs: Iterable[tuple[str, Path]]) -> None:
        """并发把若干个本地文件推到各自的远端路径。"""
        semaphore = asyncio.Semaphore(TRANSFER_FILES)

        async def one(remote: str, source: Path) -> None:
            """推一个文件，但先排队拿到并发名额。"""
            async with semaphore:
                await self.send_file(remote, source)

        await asyncio.gather(*(one(remote, source) for remote, source in pairs))

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
        """掉线的说法要带上原因。"""
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
            message_id = "@ready"
        elif op == "out":
            callback = self._streams.get(message_id)
            if callback is not None:
                callback(int(message.get("fd", 1)), base64.b64decode(message["b64"]))
            return
        elif op == "chunk":
            self._collect.setdefault(message_id, []).append(
                base64.b64decode(message["b64"])
            )
            return
        elif op == "exit":
            self._streams.pop(message_id, None)
            exited = self._exits.pop(message_id, None)
            if exited is not None and not exited.done():
                exited.set_result(int(message.get("code", -1)))
            return
        future = self._pending.pop(message_id, None)
        if future is None or future.done():
            return
        if op == "error":
            self._collect.pop(message_id, None)
            future.set_exception(RemoteError(message.get("error", "remote error")))
        else:
            future.set_result(message)

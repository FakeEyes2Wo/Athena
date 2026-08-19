"""控制节点这一半：一条常驻通道，一行一条 JSON。

``RemoteTransport`` 把「怎么连上去」和「连上之后说什么」分开，理由不是抽象洁癖：
真正的 ``ssh`` 那一跳无法在开发机上验证，而协议、流式、取消、EOF 清理、镜像增量
全都可以——只要把同一份 ``agent.py`` 用本地子进程拉起来。于是有两个实现：

- ``SubprocessTransport``：本地起同一份 agent，测试用，**协议全覆盖**。
- ``SshTransport``（见 ``ssh.py``）：``ssh <host> python3 -c <base64 源码>``，只多了一跳。

留在 ssh 那一跳之外未被覆盖的，只剩下 ssh 命令行本身——它短到可以一眼看完。
"""

import asyncio
import base64
import json
from collections.abc import Callable
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


class SubprocessTransport:
    """本地子进程跑 agent；测试与「控制节点即计算节点」都用它。"""

    def __init__(self, python: str) -> None:
        self._python = python
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def description(self) -> str:
        return f"subprocess:{self._python}"

    async def start(self) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
        self._proc = await asyncio.create_subprocess_exec(
            self._python,
            "-u",
            "-c",
            bootstrap_code(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=LINE_LIMIT_BYTES,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None
        return self._proc.stdin, self._proc.stdout

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
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
    """把 agent 源码打包成一条可以直接交给 ``python3 -c`` 的字符串。

    **不能用 ``python3 -`` 从 stdin 读源码**：解释器会把整个 stdin 当程序读完，
    stdin 也就没了——而 stdin 正是协议通道本身，同时还是「连接一断即 EOF、
    远端自己清场」这条机制的全部依据。

    源码经 base64 之后只剩 ``A-Za-z0-9+/=``，穿过 ssh 那一层 shell 不需要任何
    转义技巧；也因此不必在远端选一个可写位置落脚本、不会有版本漂移、
    通道死掉也不留下一份没人管的文件。
    """
    payload = base64.b64encode(agent_source()).decode("ascii")
    return f"import base64;exec(base64.b64decode('{payload}'))"


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
        request_id = self._next_id()
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"op": op, "id": request_id, **fields})
        return await future

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

    async def write_file(
        self, path: str, data: bytes, *, executable: bool = False
    ) -> None:
        """把字节写到远端一个路径；大文件自动分块，避免一条巨行卡住通道。"""
        limit = agent_module.CHUNK_BYTES
        blocks = [data[i : i + limit] for i in range(0, len(data), limit)] or [b""]
        for index, block in enumerate(blocks):
            await self.request(
                "put",
                path=path,
                b64=base64.b64encode(block).decode("ascii"),
                append=index > 0,
                executable=executable and index == len(blocks) - 1,
            )

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
            self._fail_pending(
                RemoteError(f"remote channel closed unexpectedly ({self.description})")
            )

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

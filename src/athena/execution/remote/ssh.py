"""SSH 传输与远程执行后端。"""

import asyncio
import hashlib
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from athena.core.contracts import ArtifactStore
from athena.execution.remote.channel import (
    LINE_LIMIT_BYTES,
    RemoteChannel,
    RemoteError,
    StderrTail,
    bootstrap_code,
)
from athena.execution.runtime import (
    BoundedOutput,
    CommandRequest,
    CommandResult,
    _dispatch,
)

# Win32-OpenSSH 会把远端命令静默截断在 8189 字节；留一半余量，越界在本地就红。
REMOTE_COMMAND_LIMIT_BYTES = 4096

# ssh 固定选项：不弹口令提示、不转发 agent、60s×3 探测掉线、转发失败即退出。
SSH_OPTIONS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ForwardAgent=no",
    "-o",
    "ServerAliveInterval=60",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "ExitOnForwardFailure=yes",
)

# 只转发语言环境；本地的 PATH/SSL_CERT_FILE 等宿主变量在远端是无效路径。
_FORWARDED_HOST_VARS: frozenset[str] = frozenset({"LANG", "LC_ALL", "LC_CTYPE"})


@dataclass(frozen=True, slots=True)
class SshHost:
    """一台可 ssh 的计算机器。"""

    name: str
    alias: str
    scratch: str = "/scratch/athena"
    python: str = "python3"
    gpus: tuple[int, ...] | None = None
    max_leases: int = 1
    options: tuple[str, ...] = field(default_factory=tuple)

    def ssh_argv(self) -> list[str]:
        """完整的 ssh 命令行：连上去并把远端 agent 拉起来。"""
        remote_command = (
            f"{shlex.quote(self.python)} -u -c {shlex.quote(bootstrap_code())}"
        )
        size = len(remote_command.encode("utf-8"))
        if size > REMOTE_COMMAND_LIMIT_BYTES:
            raise ValueError(
                f"remote command for {self.name} is {size} bytes, over the "
                f"{REMOTE_COMMAND_LIMIT_BYTES} limit; ssh truncates it silently"
            )
        return [
            "ssh",
            *SSH_OPTIONS,
            *self.options,
            self.alias,
            remote_command,
        ]


class SshTransport:
    """经系统 ``ssh`` 拉起远端 agent 的传输。"""

    def __init__(self, host: SshHost) -> None:
        self._host = host
        self._proc: Any = None
        self._stderr = StderrTail()

    @property
    def description(self) -> str:
        """连接描述：走的是哪个 ssh 别名。"""
        return f"ssh:{self._host.alias}"

    def diagnostics(self) -> str:
        """ssh 自己的 stderr——认证失败、连不上、远端 shell 报错都只在这里。"""
        return self._stderr.text()

    async def start(self) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
        """起 ssh 进程，返回它的 ``(stdin, stdout)``。"""
        argv = self._host.ssh_argv()
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT_BYTES,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._stderr.attach(self._proc.stderr)
        return self._proc.stdin, self._proc.stdout

    async def stop(self) -> None:
        """杀掉 ssh 进程。"""
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


class SshBackend:
    """在一台远程 GPU 机上执行命令。"""

    def __init__(
        self,
        host: SshHost,
        *,
        channel: RemoteChannel,
        remote_workspace: str,
        gpu_ids: tuple[int, ...] = (),
        store: ArtifactStore | None = None,
    ) -> None:
        self._host = host
        self._channel = channel
        self._workspace = PurePosixPath(remote_workspace)
        self._data_root: str | None = None
        self._gpu_ids = gpu_ids
        self._store = store

    @property
    def name(self) -> str:
        """主机名——它要进实验证据的 ``placement`` 块。"""
        return self._host.name

    @property
    def remote_workspace(self) -> str:
        """远端工作区根。"""
        return str(self._workspace)

    @property
    def channel(self):
        """底层常驻通道（数据分发等旁路操作要用）。"""
        return self._channel

    def set_data_root(self, remote_data_root: str) -> None:
        """把数据集根指到分发完成的那个内容寻址目录。"""
        self._data_root = remote_data_root

    def env_ref(self, name: str) -> str:
        """远端是 POSIX，环境变量就是 ``$NAME``。"""
        return f"${name}"

    def describe(self, workspace_root: str | Path) -> str:
        """Runtime 块——描述的是**远端**，不是控制节点。"""
        facts = self._channel.ready
        python = facts.get("python") or "missing"
        lines = [
            "Runtime:",
            f"- OS: {facts.get('os', 'Linux')} (remote host {self._host.name})",
            f"- Shell: {Path(facts.get('shell') or '/bin/bash').name}",
            f"- Workspace: {self._workspace}",
            f"- Python: {python} (this host's interpreter, used as-is)",
        ]
        lines.extend(self._environment_lines())
        if self._gpu_ids:
            names = {
                gpu.get("index"): gpu.get("name") for gpu in facts.get("gpus") or []
            }
            leased = ", ".join(
                f"{index} ({names.get(index, 'GPU')})" for index in self._gpu_ids
            )
            lines.append(
                f"- GPUs leased to this experiment: {leased}. "
                "CUDA_VISIBLE_DEVICES is already set; do not change it."
            )
        if self._data_root is not None:
            lines.append(
                '- Dataset directory: "$ATHENA_DATA_ROOT" in the shell, '
                'os.environ["ATHENA_DATA_ROOT"] in Python. '
                "Never hardcode an absolute dataset path: it differs per machine."
            )
        return "\n".join(lines)

    def _environment_lines(self) -> list[str]:
        """把"这台机器上有什么、不能装什么"说死。"""
        packages = self._channel.ready.get("packages") or {}
        installed = ", ".join(f"{name} {version}" for name, version in packages.items())
        return [
            (
                f"- Installed: {installed}"
                if installed
                else "- Installed: (none detected)"
            ),
            "- You cannot install packages on this host, and Athena will not do it "
            "for you. If an import is missing, change the approach instead.",
        ]

    def ensure_environment(self) -> None:
        """远端没有 Athena 管的环境——解释器是现成的（见 ``_environment_lines``）。"""
        return None

    def build_env(self) -> dict[str, str]:
        """远端子进程的环境。"""
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _FORWARDED_HOST_VARS
        }
        env["PATH"] = str(self._channel.ready.get("path") or "")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if self._data_root is not None:
            env["ATHENA_DATA_ROOT"] = self._data_root
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in self._gpu_ids)
        return env

    async def run(
        self,
        *,
        workspace_root: Path,
        request: CommandRequest,
    ) -> CommandResult:
        """在远端执行一条命令，流式回传输出，超时/取消都杀整个进程组。"""
        if request.predict_features is not None:
            raise NotImplementedError(
                "ATHENA_PREDICT_FEATURES is not supported on the ssh backend: "
                "the control node's path does not resolve on the remote host. "
                "Run platform-split projects with --compute local, or stage the "
                "split under the remote data root first."
            )
        workdir = (
            Path(request.workdir) if request.workdir is not None else workspace_root
        )
        try:
            relative = workdir.resolve().relative_to(Path(workspace_root).resolve())
            cwd = str(self._workspace / PurePosixPath(relative.as_posix()))
        except (ValueError, OSError):
            cwd = str(self._workspace)
        # Ensure the remote working directory exists before spawning; this also
        # makes a backend usable without an explicitly bound local root.
        await self._channel.request("mkdir", path=cwd)
        display = (
            " ".join(request.argv)
            if request.argv is not None
            else (request.command or "")
        )
        emit = request.emit
        await _dispatch(emit, "command/started", "exec:run", {"command": display})

        keep_full = self._store is not None
        sinks = {
            1: BoundedOutput(keep_full=keep_full),
            2: BoundedOutput(keep_full=keep_full),
        }
        digest = hashlib.sha256()
        pending: list[asyncio.Task] = []

        def on_output(fd: int, block: bytes) -> None:
            """收下远端的一块原始输出：进 hash、进有界缓冲、流给上层。"""
            digest.update(block)
            text = block.decode("utf-8", "replace")
            kind = "command/stdout" if fd == 1 else "command/stderr"
            sinks[fd].append(text)
            if emit is not None:
                pending.append(
                    asyncio.ensure_future(
                        _dispatch(emit, kind, "exec:out", {"delta": text})
                    )
                )

        try:
            env = self.build_env()
            if request.evaluation_split is not None:
                env["ATHENA_EVALUATION_SPLIT"] = request.evaluation_split
            job_id, exited = await self._channel.spawn(
                argv=request.argv,
                command=request.command,
                cwd=cwd,
                env=env,
                on_output=on_output,
                shell=self._channel.ready.get("shell"),
            )
        except RemoteError as exc:
            return CommandResult(
                ok=False, stdout="", stderr=str(exc), exit_code=-1, error=str(exc)
            )

        try:
            code = await asyncio.wait_for(exited, timeout=request.timeout_s)
            error = None
        except asyncio.TimeoutError:
            await self._channel.cancel(job_id)
            code, error = -1, "timeout"
        except asyncio.CancelledError:
            await self._channel.cancel(job_id)
            raise
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        out, err = sinks[1], sinks[2]
        truncated = out.truncated or err.truncated
        output_ref = None
        if self._store is not None and (code != 0 or truncated):
            output_ref = await self._store.put_text(
                "\n".join(part for part in (out.full_text(), err.full_text()) if part)
            )
        elif truncated:
            output_ref = "sha256:" + digest.hexdigest()
        result = CommandResult(
            ok=code == 0 and error is None,
            stdout=out.text(output_ref),
            stderr=err.text(output_ref),
            exit_code=code,
            error=error,
            truncated=truncated,
            output_ref=output_ref,
        )
        await _dispatch(emit, "command/completed", "exec:run", result.to_dict())
        return result

    async def aclose(self) -> None:
        """关掉常驻通道。"""
        await self._channel.close()

"""SSH 传输与远程执行后端。

**不引 Python SSH 库，直接调系统 ``ssh``。** 理由（设计文档 §4.4）：

- ``~/.ssh/config`` 全部生效——``ProxyJump``（集群堡垒机的常态）、``IdentityFile``、
  端口、``known_hosts``、硬件密钥 / ssh-agent，一样都不用重新实现。
- 配置里只写一个 **Host 别名**，不写用户名、IP、端口、密钥路径。Athena 的配置
  文件里因此永远不会出现凭据。
- 不新增依赖，也就不新增 CVE 面。

代价明写：Win32-OpenSSH 不支持 ControlMaster 连接复用。所以这里**不是**每条命令
起一个 ssh，而是一份租约一条常驻通道（见 ``channel.py``），握手只付一次，两端
行为一致。
"""

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from athena.core.contracts import ArtifactStore
from athena.core.tool_types import EmitEvent
from athena.execution.remote.channel import (
    LINE_LIMIT_BYTES,
    RemoteChannel,
    RemoteError,
    bootstrap_code,
)
from athena.execution.runtime import CommandResult, MAX_OUTPUT_CHARS, _dispatch

# ssh 的固定选项。逐条都有理由，别当样板删：
# - BatchMode：绝不弹交互式口令提示。通道跑在后台，提示只会变成一次静默挂死。
# - ForwardAgent=no：agent 写的代码要在那台机器上跑，不能顺手拿到本地的 ssh 身份。
# - ServerAliveInterval/CountMax：60s×3 内确认掉线，让远端 stdin 尽快 EOF，
#   显存不被跑飞的训练进程一直占着。
# - ExitOnForwardFailure：转发失败就退出，而不是留一条半残的通道。
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


@dataclass(frozen=True, slots=True)
class SshHost:
    """一台可 ssh 的计算机器。

    ``alias`` 是 ``~/.ssh/config`` 里的 Host 别名，不是 ``user@ip``——连接的复杂度
    （跳板、端口、密钥）留在唯一有资格管它的地方。
    """

    name: str
    alias: str
    scratch: str = "/scratch/athena"
    python: str = "python3"
    gpus: tuple[int, ...] | None = None
    max_leases: int = 1
    options: tuple[str, ...] = field(default_factory=tuple)

    def ssh_argv(self) -> list[str]:
        """完整的 ssh 命令行：连上去并把远端 agent 拉起来。"""
        return [
            "ssh",
            *SSH_OPTIONS,
            *self.options,
            self.alias,
            f"{shlex.quote(self.python)} -u -c {shlex.quote(bootstrap_code())}",
        ]


class SshTransport:
    """经系统 ``ssh`` 拉起远端 agent 的传输。"""

    def __init__(self, host: SshHost) -> None:
        self._host = host
        self._proc: Any = None

    @property
    def description(self) -> str:
        return f"ssh:{self._host.alias}"

    async def start(self):
        import asyncio

        argv = self._host.ssh_argv()
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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


# 转发到远端的宿主环境变量：几乎为空。
#
# 本地那份白名单（PATH/HOME/TEMP/UV_CACHE_DIR/SSL_CERT_FILE…）在远端全是无效路径，
# SSL_CERT_FILE 还会让远端 TLS 直接失败。语言环境是唯一无害且有用的一类。
_FORWARDED_HOST_VARS: frozenset[str] = frozenset({"LANG", "LC_ALL", "LC_CTYPE"})


class SshBackend:
    """在一台远程 GPU 机上执行命令。

    它**不是**完整的 ``ExecutionBackend``：少一个 ``collect_outputs``。这是有意的。
    远程执行绕不开「文件怎么在两台机器之间搬」，而搬法有策略、有代价，必须由
    ``MirroredBackend`` 显式包一层才算完整。留一个空的 ``collect_outputs`` 在这里
    会让「产出没拉回来」变成一次静默的空目录，而不是一个类型错误。

    一个 Plan 拿到租约后，它的**全部**命令——``shell_command`` 与 manifest——都落在
    这台机器的同一个目录。
    """

    def __init__(
        self,
        host: SshHost,
        *,
        channel: RemoteChannel,
        remote_workspace: str,
        remote_env_root: str,
        remote_data_root: str | None = None,
        gpu_ids: tuple[int, ...] = (),
        store: ArtifactStore | None = None,
    ) -> None:
        self._host = host
        self._channel = channel
        self._workspace = PurePosixPath(remote_workspace)
        self._env_root = remote_env_root
        self._data_root = remote_data_root
        self._gpu_ids = gpu_ids
        self._store = store

    @property
    def name(self) -> str:
        """主机名——它要进实验证据的 ``placement`` 块。"""
        return self._host.name

    @property
    def facts(self) -> dict[str, Any]:
        """远端握手时报上来的事实（os/python/uv/GPU 列表）。"""
        return dict(self._channel.ready)

    @property
    def remote_workspace(self) -> str:
        """远端工作区根。"""
        return str(self._workspace)

    @property
    def channel(self):
        """底层常驻通道（数据分发等旁路操作要用）。"""
        return self._channel

    def set_data_root(self, remote_data_root: str) -> None:
        """把数据集根指到分发完成的那个内容寻址目录。

        必须在起任何命令之前调用：``build_env`` 会把它写进 ``ATHENA_DATA_ROOT``，
        而 agent 生成的脚本只认这个变量。
        """
        self._data_root = remote_data_root

    def env_ref(self, name: str) -> str:
        """远端是 POSIX，环境变量就是 ``$NAME``。"""
        return f"${name}"

    def describe(self, workspace_root: str | Path) -> str:
        """Runtime 块——描述的是**远端**，不是控制节点。

        这正是那条不变式的落点：本机是 Windows/PowerShell，这里必须说 Linux/bash，
        否则模型会给 bash 写 PowerShell，而报错只会显示成语法错误。
        """
        facts = self._channel.ready
        python = facts.get("python") or "missing"
        state = "environment ready" if facts.get("uv") else "uv not found on the host"
        lines = [
            "Runtime:",
            f"- OS: {facts.get('os', 'Linux')} (remote host {self._host.name})",
            f"- Shell: {Path(facts.get('shell') or '/bin/bash').name}",
            f"- Workspace: {self._workspace}",
            f"- Python: {python}, {state}",
        ]
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
        lines.append(
            '- Add dependencies with: uv add --project "$ATHENA_ENV_ROOT" <package>'
        )
        return "\n".join(lines)

    def ensure_environment(self) -> None:
        """远端环境根的准备由租约建立时完成（见 ``prepare_remote``）。"""
        return None

    async def prepare_remote(self) -> None:
        """在远端建好工作区与环境根目录。租约建立时调一次。"""
        for path in (str(self._workspace), self._env_root):
            await self._channel.request("mkdir", path=path)

    def build_env(self) -> dict[str, str]:
        """远端子进程的环境。

        刻意**不**转发本地的 PATH/HOME/TEMP/SSL_CERT_FILE 之类：那些值在远端全是
        无效路径。只留语言环境，加上 Athena 自己的三个根与租到的卡。
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _FORWARDED_HOST_VARS
        }
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["ATHENA_ENV_ROOT"] = self._env_root
        if self._data_root is not None:
            env["ATHENA_DATA_ROOT"] = self._data_root
        # 卡的分配是 Athena 自己做的（裸机没有调度器），靠它强制。
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in self._gpu_ids)
        return env

    async def run(
        self,
        *,
        command: str | None = None,
        argv: list[str] | None = None,
        workspace_root: Path,
        workdir: Path,
        timeout_s: int,
        emit: EmitEvent | None = None,
    ) -> CommandResult:
        """在远端执行一条命令，流式回传输出，超时/取消都杀整个进程组。"""
        import asyncio

        del workspace_root  # 远端路径由镜像决定，本地根在这里没有意义
        cwd = self._remote_cwd(workdir)
        display = " ".join(argv) if argv is not None else (command or "")
        await _dispatch(emit, "command/started", "exec:run", {"command": display})

        heads: dict[int, list[str]] = {1: [], 2: []}
        lengths: dict[int, int] = {1: 0, 2: 0}
        pending: list[asyncio.Task] = []

        def on_output(fd: int, block: bytes) -> None:
            # 远端固定 UTF-8：不套用本地代码页回退，那是控制节点的事。
            text = block.decode("utf-8", "replace")
            kind = "command/stdout" if fd == 1 else "command/stderr"
            if lengths[fd] < MAX_OUTPUT_CHARS:
                room = MAX_OUTPUT_CHARS - lengths[fd]
                heads[fd].append(text[:room])
                lengths[fd] = min(MAX_OUTPUT_CHARS, lengths[fd] + len(text))
            if emit is not None:
                pending.append(
                    asyncio.ensure_future(
                        _dispatch(emit, kind, "exec:out", {"delta": text})
                    )
                )

        try:
            job_id, exited = await self._channel.spawn(
                argv=argv,
                command=command,
                cwd=cwd,
                env=self.build_env(),
                on_output=on_output,
                shell=self._channel.ready.get("shell"),
            )
        except RemoteError as exc:
            return CommandResult(
                ok=False, stdout="", stderr=str(exc), exit_code=-1, error=str(exc)
            )

        try:
            code = await asyncio.wait_for(exited, timeout=timeout_s)
            error = None
        except asyncio.TimeoutError:
            await self._channel.cancel(job_id)
            code, error = -1, "timeout"
        except asyncio.CancelledError:
            # turn 被取消 → 远端进程组必须一起走，否则显存留给一个没人要的作业
            await self._channel.cancel(job_id)
            raise
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        stdout, stderr = "".join(heads[1]), "".join(heads[2])
        truncated = lengths[1] >= MAX_OUTPUT_CHARS or lengths[2] >= MAX_OUTPUT_CHARS
        output_ref = None
        if self._store is not None and (code != 0 or truncated):
            output_ref = await self._store.put_text(
                "\n".join(part for part in (stdout, stderr) if part)
            )
        result = CommandResult(
            ok=code == 0 and error is None,
            stdout=stdout,
            stderr=stderr,
            exit_code=code,
            error=error,
            truncated=truncated,
            output_ref=output_ref,
        )
        await _dispatch(emit, "command/completed", "exec:run", result.to_dict())
        return result

    async def aclose(self) -> None:
        """关掉常驻通道。远端 stdin 因此 EOF，它会杀掉自己起过的所有进程组。"""
        await self._channel.close()

    def _remote_cwd(self, workdir: Path) -> str:
        """把本地 workdir 折算成远端路径（工作区之外的一律落回工作区根）。"""
        try:
            relative = (
                Path(workdir).resolve().relative_to(Path(self._local_root()).resolve())
            )
        except (ValueError, OSError):
            return str(self._workspace)
        return str(self._workspace / PurePosixPath(relative.as_posix()))

    def _local_root(self) -> Path:
        """本地工作区根；由 ``bind_local_root`` 设定。"""
        return getattr(self, "_local_workspace", Path.cwd())

    def bind_local_root(self, local_root: Path) -> None:
        """记住本地工作区根，用于把 workdir 折算成远端路径。"""
        self._local_workspace = Path(local_root)

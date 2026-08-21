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

import asyncio
import hashlib
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
    StderrTail,
    bootstrap_code,
)
from athena.execution.runtime import BoundedOutput, CommandResult, _dispatch

# 远端命令的长度上限。**这是量出来的，不是估的**：Win32-OpenSSH 9.5 把远端命令
# 静默截断在 8189 字节——退出码仍是 0，远端 bash 只抱怨引号没配对，看起来像
# 转义写错了，而不是"太长了"。控制节点是 Windows 是本设计锁定的前提，所以宁可
# 在本地当场红。留一半余量给 shell 自己那点开销。
REMOTE_COMMAND_LIMIT_BYTES = 4096

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

# 转发到远端的宿主环境变量：几乎为空。
#
# 本地那份白名单（PATH/HOME/TEMP/UV_CACHE_DIR/SSL_CERT_FILE…）在远端全是无效路径，
# SSL_CERT_FILE 还会让远端 TLS 直接失败。语言环境是唯一无害且有用的一类。
_FORWARDED_HOST_VARS: frozenset[str] = frozenset({"LANG", "LC_ALL", "LC_CTYPE"})


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
        """完整的 ssh 命令行：连上去并把远端 agent 拉起来。

        命令必须短。agent 源码走 stdin 的头一段而不是命令行，正是因为
        ``REMOTE_COMMAND_LIMIT_BYTES`` 那条真机限制。这里再守一道，是因为它的
        违反方式是**静默截断**：越界不会报错，只会让远端执行半条命令。
        """
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
        # 必须后台抽干：管道写满之后 ssh 会阻塞在写 stderr 上，表现成"连上了没反应"。
        self._stderr.attach(self._proc.stderr)
        return self._proc.stdin, self._proc.stdout

    async def stop(self) -> None:
        """杀掉 ssh 进程。远端 stdin 因此 EOF，它会自己清场。"""
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
        remote_data_root: str | None = None,
        gpu_ids: tuple[int, ...] = (),
        store: ArtifactStore | None = None,
    ) -> None:
        self._host = host
        self._channel = channel
        self._workspace = PurePosixPath(remote_workspace)
        self._data_root = remote_data_root
        self._gpu_ids = gpu_ids
        self._store = store
        # 本地工作区根，由 bind_local_root 设定；用来把 workdir 折算成远端路径。
        self._local_workspace = Path.cwd()

    @property
    def name(self) -> str:
        """主机名——它要进实验证据的 ``placement`` 块。"""
        return self._host.name

    @property
    def facts(self) -> dict[str, Any]:
        """远端握手时报上来的事实（os/shell/python/PATH/现成包/GPU 列表）。"""
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
        del workspace_root  # 远端工作区由租约给定，本地根在这里没有意义
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
        """把"这台机器上有什么、不能装什么"说死。

        **远端只用现成的解释器，Athena 不在那边管环境。** 这是一个明确的取舍，
        不是没做完：

        - 装依赖要写盘、要联网、可能要 sudo，还会在一台可能被别人共用的机器上改
          全局状态。这几样在租来的 GPU 机上都不该由 agent 顺手做。
        - 真机上第一台机器就没有 uv，而原本那条无条件的
          ``uv add --project "$ATHENA_ENV_ROOT"`` 指向的还是一个空目录——
          教了一条必然失败的命令。
        - 装包的失败模式很脏：半装上的依赖会让下一个租到这台机器的实验跑在一份
          谁也说不清的环境里，而证据里不会留下任何痕迹。

        代价是 agent 只能用现成的包，所以**必须告诉它现成的是哪些**——否则它会
        拿一轮去试 import、再拿一轮去试装、最后拿一轮猜为什么装不上。
        """
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

    async def prepare_remote(self) -> None:
        """在远端建好工作区。租约建立时调一次。

        只建工作区：远端**没有**环境根。本地那个 ``ATHENA_ENV_ROOT``（一个 uv
        项目）在远端没有对应物，凭空建一个空目录只会让人以为那边也有一份受管环境。
        """
        await self._channel.request("mkdir", path=str(self._workspace))

    def remote_path(self) -> str:
        """远端子进程的 PATH——就是远端自己那一份。

        **必须问远端要**，不能拿本地的 PATH 去改：非交互式 ssh 不读 ``~/.bashrc``，
        容器里 conda 那一段 PATH 因此不在——真机上量到的表现是
        ``ssh host python3 -V`` 直接 command not found，而交互登录一切正常。
        远端 agent 在 ``probe`` 里把自己解释器的 bin 目录顶到最前再报上来。

        这里**不**像本地执行器那样前置任何 ``.venv/bin``：远端没有 Athena 管的
        环境（见 ``_environment_lines``），指向一个我们从不创建的目录只是装样子。
        """
        return str(self._channel.ready.get("path") or "")

    def build_env(self) -> dict[str, str]:
        """远端子进程的环境。

        刻意**不**转发本地的 PATH/HOME/TEMP/SSL_CERT_FILE 之类：那些值在远端全是
        无效路径。只留语言环境，加上远端自己的 PATH、数据集根，以及租到的卡。

        **没有 ``ATHENA_ENV_ROOT``**：远端没有 Athena 管的环境。设一个指向空目录
        的变量，只会让 agent 以为那边可以 ``uv add``。
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _FORWARDED_HOST_VARS
        }
        env["PATH"] = self.remote_path()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
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
        del workspace_root  # 远端路径由镜像决定，本地根在这里没有意义
        cwd = self._remote_cwd(workdir)
        display = " ".join(argv) if argv is not None else (command or "")
        await _dispatch(emit, "command/started", "exec:run", {"command": display})

        # keep_full 与本地执行器同一条规则：有地方存全文才留全文。留了才敢在
        # 结果里给 output_ref——那个 ref 承诺"完整输出在这里"。
        keep_full = self._store is not None
        sinks = {
            1: BoundedOutput(keep_full=keep_full),
            2: BoundedOutput(keep_full=keep_full),
        }
        digest = hashlib.sha256()
        pending: list[asyncio.Task] = []

        def on_output(fd: int, block: bytes) -> None:
            """收下远端的一块原始输出：进 hash、进有界缓冲、流给上层。"""
            # 远端固定 UTF-8：不套用本地代码页回退，那是控制节点的事。
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

        out, err = sinks[1], sinks[2]
        truncated = out.truncated or err.truncated
        output_ref = None
        if self._store is not None and (code != 0 or truncated):
            # 存的必须是**完整**输出。存被砍过的那份等于给了一个骗人的 ref：
            # agent 拿着它去查 traceback，查到的还是被砍掉 traceback 的那份。
            output_ref = await self._store.put_text(
                "\n".join(part for part in (out.full_text(), err.full_text()) if part)
            )
        elif truncated:
            output_ref = "sha256:" + digest.hexdigest()
        result = CommandResult(
            ok=code == 0 and error is None,
            # ref 先算再渲染：省略标记要把"去哪儿取全的"写进正文。
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
        """关掉常驻通道。远端 stdin 因此 EOF，它会杀掉自己起过的所有进程组。"""
        await self._channel.close()

    def _remote_cwd(self, workdir: Path) -> str:
        """把本地 workdir 折算成远端路径（工作区之外的一律落回工作区根）。"""
        try:
            relative = (
                Path(workdir).resolve().relative_to(self._local_workspace.resolve())
            )
        except (ValueError, OSError):
            # workdir 不在工作区里 / 路径解析不了 → 落回工作区根，不猜
            return str(self._workspace)
        return str(self._workspace / PurePosixPath(relative.as_posix()))

    def bind_local_root(self, local_root: Path) -> None:
        """记住本地工作区根，用于把 workdir 折算成远端路径。"""
        self._local_workspace = Path(local_root)

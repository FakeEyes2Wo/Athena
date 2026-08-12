"""共享执行运行时：统一 ``shell_command`` 工具与直接执行 API。

对齐 codex_docs/2026-08-11-shared-execution-runtime-design.md 首版范围。
``ExecutionRuntime`` 是唯一入口：选择环境 → 执行命令 → 流式事件 → 紧凑结果。
``EnvironmentManager`` 负责平台/shell/python/uv/git 探测与子进程环境构造；
``CommandExecutor`` 负责起进程、流式读取、超时/取消与有界输出。本模块不含任何
EDA/训练/评估业务决策；安全隔离与远程执行是显式未来工作。
"""

import asyncio
import hashlib
import os
import shutil
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from athena.core.contracts import ArtifactStore
from athena.core.tool import BaseTool
from athena.core.tool_types import EmitEvent, ToolContext, ToolSpec

# 有界输出上限（字符）：超出后截断并记录完整输出 hash，避免无限累积。
MAX_OUTPUT_CHARS = 60_000

# 子进程保留的宿主环境变量白名单（过滤凭据类，避免脚本读取宿主机密）。
# PATHEXT/COMSPEC 缺失时 PowerShell 无法按扩展名解析 python.exe（"未识别为 cmdlet"）。
_HOST_VARS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)

# Windows shell 探测顺序：Git Bash（agent 脚本为 POSIX/bash）→ PowerShell 7 →
# 5.1 → cmd → PATH 上的 bash/sh；绝不自动路由到 WSL。
_WIN_SHELLS: tuple[tuple[str, list[str]], ...] = (
    (
        r"C:\Program Files\Git\bin\bash.exe",
        ["--noprofile", "-c"],
    ),
    (
        r"C:\Program Files\Git\usr\bin\bash.exe",
        ["--noprofile", "-c"],
    ),
    ("bash", ["--noprofile", "-c"]),
    ("sh", ["-c"]),
    (
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        ["-NoProfile", "-NonInteractive", "-Command"],
    ),
    (
        r"C:\Program Files\PowerShell\7-preview\pwsh.exe",
        ["-NoProfile", "-NonInteractive", "-Command"],
    ),
    (
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        ["-NoProfile", "-NonInteractive", "-Command"],
    ),
    (r"C:\Windows\System32\cmd.exe", ["/d", "/s", "/c"]),
)
# Linux shell 探测顺序：bash → sh。
_POSIX_SHELLS: tuple[tuple[str, list[str]], ...] = (
    ("bash", ["--noprofile", "-c"]),
    ("sh", ["-c"]),
)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """一次命令执行的上下文：三个根路径 + 可选 experiment_id。"""

    project_root: Path
    workspace_root: Path
    environment_root: Path
    experiment_id: str | None = None


@dataclass(slots=True)
class CommandResult:
    """命令执行结果（模型可见的紧凑视图）。"""

    ok: bool
    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None
    truncated: bool = False
    output_ref: str | None = None

    def to_dict(self) -> dict[str, object]:
        """转换为工具返回的 JSON 形状；error/truncated 仅在有值时出现。"""
        data: dict[str, object] = {
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
        }
        if self.error is not None:
            data["error"] = self.error
        if self.truncated:
            data["truncated"] = True
            if self.output_ref is not None:
                data["output_ref"] = self.output_ref
        return data


def _validate_command_input(command: str | None, argv: list[str] | None) -> None:
    if (command is None) == (argv is None):
        raise ValueError("exactly one of command or argv must be provided")
    if command is not None and not command.strip():
        raise ValueError("command must be a non-empty string")
    if argv is not None and (
        not argv or any(not isinstance(part, str) or not part for part in argv)
    ):
        raise ValueError("argv must be a non-empty list of non-empty strings")


class EnvironmentManager:
    """平台/shell/工具探测、子进程环境构造与运行时摘要。"""

    def __init__(
        self,
        *,
        project_root: str | Path,
        environment_root: str | Path,
        host: dict[str, str] | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._environment_root = Path(environment_root)
        self._host = host if host is not None else os.environ
        self._versions: dict[str, str] = {}
        self._needs_repair: str | None = None

    @property
    def os_name(self) -> str:
        """平台显示名（Windows / Linux）。"""
        return "Windows" if os.name == "nt" else "Linux"

    def shell_parts(self) -> tuple[str, list[str]]:
        """返回 ``(shell 绝对路径, 启动参数)``。

        Windows 优先 Git Bash（agent 脚本为 POSIX/bash）→ PowerShell → cmd；
        Linux 顺序 bash → sh。name 候选经 ``shutil.which`` 解析，绝对路径经
        ``is_file`` 校验。探测失败抛 ``RuntimeError``（由调用方决定降级或报错）。
        """
        pool = _WIN_SHELLS if os.name == "nt" else _POSIX_SHELLS
        for shell, args in pool:
            if Path(shell).is_absolute():
                if Path(shell).is_file():
                    return shell, args
            else:
                found = shutil.which(shell)
                if found:
                    return found, args
        raise RuntimeError(f"no shell found on {self.os_name}")

    def build_env(self) -> dict[str, str]:
        """构造子进程环境：白名单宿主变量 + 环境根 venv 前置 PATH + UTF-8。

        ``ATHENA_ENV_ROOT`` 指向含 ``pyproject.toml``/``uv.lock`` 的环境根，
        供 agent 用 ``uv add --project "$ATHENA_ENV_ROOT"`` 动态加依赖。
        """
        env = {
            key.upper(): value
            for key, value in self._host.items()
            if key.upper() in _HOST_VARS
        }
        bindir = (
            self._environment_root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        )
        entries: list[str] = [str(bindir)] if bindir.is_dir() else []
        entries.extend(p for p in env.get("PATH", "").split(os.pathsep) if p)
        env["PATH"] = os.pathsep.join(dict.fromkeys(entries))
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["ATHENA_ENV_ROOT"] = str(self._environment_root)
        return env

    def tool_versions(self) -> dict[str, str]:
        """探测 python/uv/git 版本（带缓存；不可用时标记 missing）。"""
        for name in ("python", "uv", "git"):
            if name not in self._versions:
                self._versions[name] = self._probe(name)
        return self._versions

    def environment_hash(self) -> str:
        """环境快照哈希：pyproject.toml + uv.lock + python 版本（刷新失效判定）。

        声明文件任一变化（或 python 版本变化）即产生不同哈希，供缓存失效判断
        （design §Refresh Rules）。不读取虚拟环境本身。
        """
        digest = hashlib.sha256()
        for name in ("pyproject.toml", "uv.lock"):
            path = self._environment_root / name
            if path.is_file():
                digest.update(path.read_bytes())
        digest.update(self.tool_versions().get("python", "unknown").encode())
        return digest.hexdigest()[:16]

    def sync(self, *, frozen: bool = False) -> dict[str, object]:
        """在环境根运行 ``uv sync``（frozen 时 ``--frozen``）；返回 ``{ready, error?}``。

        缺 pyproject.toml 或 uv 不可用 → 明确不 ready；任何失败记录
        ``needs_repair``，不静默还原 agent 写入的文件（design §Refresh Rules）。
        """
        if not (self._environment_root / "pyproject.toml").is_file():
            return self._mark_repair("no pyproject.toml in environment_root")
        uv = shutil.which("uv")
        if not uv:
            return self._mark_repair("uv not found on PATH")
        args = ["uv", "sync"] + (["--frozen"] if frozen else [])
        try:
            proc = subprocess.run(
                args,
                cwd=str(self._environment_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return self._mark_repair("uv sync timed out")
        if proc.returncode != 0:
            return self._mark_repair((proc.stderr or proc.stdout).strip()[:200])
        self._needs_repair = None
        return {"ready": True}

    def _mark_repair(self, error: str) -> dict[str, object]:
        """记录需要修复并返回失败结果。"""
        self._needs_repair = error
        return {"ready": False, "error": error}

    @property
    def needs_repair(self) -> str | None:
        """最近一次 sync 失败原因（None = 环境就绪）。"""
        return self._needs_repair

    def runtime_summary(self, workspace_root: str | Path) -> str:
        """模型可见的简洁运行时块（注入 system prompt；失败给出可行动错误）。

        环境 sync 失败（``needs_repair``）优先于 python 探测结果，报告给 agent。
        """
        versions = self.tool_versions()
        python = versions.get("python", "missing")
        if self._needs_repair:
            state = f"environment needs repair: {self._needs_repair}"
        elif python != "missing":
            state = "environment ready"
        else:
            state = "environment needs repair: python not found"
        try:
            shell_name = Path(self.shell_parts()[0]).name
        except RuntimeError as exc:
            return f"Runtime:\n- environment needs repair: {exc}"
        return (
            "Runtime:\n"
            f"- OS: {self.os_name}\n"
            f"- Shell: {shell_name}\n"
            f"- Workspace: {workspace_root}\n"
            f"- Python: {python}, {state}\n"
            f'- Add dependencies with: uv add --project "$ATHENA_ENV_ROOT" <package>'
        )

    def _probe(self, name: str) -> str:
        found = shutil.which(name)
        if not found:
            return "missing"
        try:
            proc = subprocess.run(
                [found, "--version"], capture_output=True, text=True, timeout=5
            )
            return (proc.stdout or proc.stderr).strip().splitlines()[0]
        except (OSError, subprocess.TimeoutExpired):
            return "missing"


async def _dispatch(
    emit: EmitEvent | None, kind: str, ref: str, data: dict[str, object]
) -> None:
    """调用事件回调（兼容同步/异步回调，对齐 runtime._publish 语义）。"""
    if emit is None:
        return
    result = emit(kind, ref, data)
    if asyncio.iscoroutine(result):
        await result


class CommandExecutor:
    """在选定 shell 中执行命令；流式事件、超时/取消、进程树终止与有界输出。

    ``persist`` 可选：失败/截断命令把完整输出持久化为 artifact（design §Persistence）。
    """

    def __init__(
        self,
        *,
        env: dict[str, str],
        persist: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._env = env
        self._persist = persist

    def _spawn_flags(self) -> tuple[int, bool]:
        """Windows 用独立进程组（taskkill 可杀整树）；POSIX 用新会话。"""
        if os.name == "nt":
            return subprocess.CREATE_NEW_PROCESS_GROUP, False
        return 0, True

    @staticmethod
    def _utf8_command(shell: str, command: str) -> str:
        """PowerShell 显式设 UTF-8 输出编码并透传最后命令退出码。

        PowerShell 进程退出码默认恒为 0（即使 python 退出 3），需 ``exit
        $LASTEXITCODE`` 才反映真实结果；否则非零退出会被误判为成功。
        """
        name = Path(shell).name.lower()
        if "powershell" in name or "pwsh" in name:
            return (
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "$OutputEncoding=[Text.Encoding]::UTF8;"
                + command
                + "; exit $LASTEXITCODE"
            )
        return command

    def _terminate(self, proc) -> None:
        """终止完整子进程树（Windows ``taskkill /T``；POSIX ``killpg``）。"""
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def run(
        self,
        *,
        command: str | None = None,
        argv: list[str] | None = None,
        workdir: Path,
        shell: str | None = None,
        shell_args: list[str] | None = None,
        timeout_s: int,
        emit: EmitEvent | None = None,
    ) -> CommandResult:
        """执行命令并返回紧凑结果；命令事件经 ``emit`` 流式推送。

        ``command`` 走 shell 包装（既有 API）；``argv`` 提供时直接以 argv 启动
        可执行文件、不经 shell，供 experiment manifest 等结构化调用使用。
        """
        _validate_command_input(command, argv)
        flags, new_session = self._spawn_flags()
        display = " ".join(argv) if argv is not None else command
        await _dispatch(emit, "command/started", "exec:run", {"command": display})
        try:
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(workdir),
                    env=self._env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=flags,
                    start_new_session=new_session,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    *shell_args,
                    self._utf8_command(shell, command),
                    cwd=str(workdir),
                    env=self._env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=flags,
                    start_new_session=new_session,
                )
        except FileNotFoundError:
            # 可执行文件/shell 不存在 → 不穿透，给可行动错误
            error = "command_not_found" if argv is not None else "shell_not_found"
            return CommandResult(
                ok=False, stdout="", stderr="", exit_code=127, error=error
            )

        out_head: list[str] = []
        err_head: list[str] = []
        # persist 启用时累积完整输出（否则只保留有界头部，内存有界）。
        out_full: list[str] = [] if self._persist is not None else None
        err_full: list[str] = [] if self._persist is not None else None
        digest = hashlib.sha256()

        async def pump(
            stream: asyncio.StreamReader,
            kind: str,
            head: list[str],
            full: list[str] | None,
        ) -> int:
            """读取流式输出：边 hash 完整输出边累积有界头部，返回本流字符数。"""
            length = 0
            async for raw in stream:
                digest.update(raw)
                line = raw.decode("utf-8", errors="replace")
                await _dispatch(emit, kind, "exec:out", {"delta": line})
                if length < MAX_OUTPUT_CHARS:
                    room = MAX_OUTPUT_CHARS - length
                    head.append(line[:room])
                    length = min(MAX_OUTPUT_CHARS, length + len(line))
                if full is not None:
                    full.append(line)
            return length

        readers = [
            asyncio.create_task(
                pump(proc.stdout, "command/stdout", out_head, out_full)
            ),
            asyncio.create_task(
                pump(proc.stderr, "command/stderr", err_head, err_full)
            ),
        ]
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            self._terminate(proc)  # 超时 → 终止整个进程树
            await asyncio.gather(*readers, return_exceptions=True)
            return CommandResult(
                ok=False,
                stdout="".join(out_head),
                stderr="".join(err_head),
                exit_code=-1,
                error="timeout",
            )
        except asyncio.CancelledError:
            self._terminate(proc)  # 取消 turn → 终止整个进程树后传播
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        out_len, err_len = await asyncio.gather(*readers)

        stdout = "".join(out_head)
        stderr = "".join(err_head)
        truncated = out_len >= MAX_OUTPUT_CHARS or err_len >= MAX_OUTPUT_CHARS
        if self._persist is not None and (returncode != 0 or truncated):
            # 持久化失败/截断命令的完整日志为证据 artifact（design §Persistence Policy）
            full_text = "\n".join(
                part
                for part in (
                    "".join(out_full or []),
                    "".join(err_full or []),
                )
                if part
            )
            output_ref = await self._persist(full_text)
        elif truncated:
            output_ref = "sha256:" + digest.hexdigest()
        else:
            output_ref = None
        result = CommandResult(
            ok=returncode == 0,
            stdout=stdout,
            stderr=stderr,
            exit_code=returncode or 0,
            truncated=truncated,
            output_ref=output_ref,
        )
        await _dispatch(emit, "command/completed", "exec:run", result.to_dict())
        return result


class ExecutionRuntime:
    """共享执行运行时门面：选择环境 → 执行命令 → 流式事件 → 紧凑结果。

    扩展点（design §Deferred Work，均不静默部分实现）：
    - TODO(execution-security): 强制文件系统/进程/网络/命令策略。
    - TODO(container-executor): 在强隔离容器中执行命令。
    - TODO(remote-executor): 移到 daemon / 远程 worker 协议后。
    - TODO(environment-lock): PREPARE 并发时序列化依赖变更。
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        environment_root: str | Path | None = None,
        store: ArtifactStore | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._environment_root = (
            Path(environment_root)
            if environment_root is not None
            else self._project_root
        )
        self._store = store
        self._env = EnvironmentManager(
            project_root=self._project_root, environment_root=self._environment_root
        )

    @property
    def project_root(self) -> Path:
        """研究项目根（非 Athena 源码树）。"""
        return self._project_root

    @property
    def environment_root(self) -> Path:
        """环境根（含 pyproject.toml / uv.lock / .venv）。"""
        return self._environment_root

    def shell_command_tool(self, workspace_root: str | Path) -> BaseTool:
        """构造模型可见的 shell_command 工具（cwd 默认 workspace）。"""
        return _ShellCommandTool(self, Path(workspace_root))

    def runtime_summary(self, workspace_root: str | Path) -> str:
        """注入 system prompt 的简洁运行时块。"""
        return self._env.runtime_summary(Path(workspace_root))

    async def run(
        self,
        context: ExecutionContext,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        timeout_s: int = 120,
        workdir: str | Path | None = None,
        emit: EmitEvent | None = None,
    ) -> CommandResult:
        """直接执行 API（Supervisor/Service/Runner 复用同一语义）。

        ``command`` 走 shell（既有路径）；``argv`` 提供时直接以 argv 启动可执行
        文件、不经 shell，供 experiment manifest 等结构化调用使用。配置
        ``store`` 时，失败/截断命令的完整输出持久化为证据 artifact
        （design §Persistence Policy）。
        """
        _validate_command_input(command, argv)
        cwd = Path(workdir) if workdir is not None else context.workspace_root
        if argv is not None:
            shell, shell_args = None, None
        else:
            shell, shell_args = self._env.shell_parts()
        persist = None
        if self._store is not None:

            async def persist(full_text: str) -> str:
                """把完整命令输出写为证据 artifact，返回其 ref。"""
                return await self._store.put_text(full_text)

        executor = CommandExecutor(env=self._env.build_env(), persist=persist)
        return await executor.run(
            command=command,
            argv=argv,
            workdir=cwd,
            shell=shell,
            shell_args=shell_args,
            timeout_s=timeout_s,
            emit=emit,
        )


class _ShellCommandTool(BaseTool):
    """模型可见的 shell_command 工具（唯一命令工具，替换 bash/pwsh）。"""

    spec = ToolSpec(
        name="shell_command",
        description=(
            "Run a shell command in the workspace and return stdout/stderr/exit_code. "
            "A nonzero exit is a normal result; read stderr, fix the command, "
            "and retry in the same turn."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 120},
            },
            "required": ["command"],
        },
    )

    def __init__(self, runtime: ExecutionRuntime, workspace_root: Path) -> None:
        self._runtime = runtime
        self._workspace_root = workspace_root

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        """执行命令：委托 ``ExecutionRuntime.run`` 并返回契约 JSON。"""
        context = ExecutionContext(
            project_root=self._runtime.project_root,
            workspace_root=self._workspace_root,
            environment_root=self._runtime.environment_root,
        )
        result = await self._runtime.run(
            context,
            str(input["command"]),
            timeout_s=int(input.get("timeout_s", 120)),
            workdir=input.get("workdir"),
            emit=ctx.emit,
        )
        return result.to_dict()

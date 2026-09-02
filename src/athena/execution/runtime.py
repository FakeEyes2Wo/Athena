"""共享执行运行时：统一 ``shell_command`` 工具与直接执行 API。"""

import asyncio
import codecs
import hashlib
import locale
import os
import re
import shutil
import signal
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from athena.core.contracts import ArtifactStore
from athena.core.tool import BaseTool
from athena.core.tool_types import EmitEvent, ToolContext, ToolSpec

if TYPE_CHECKING:
    from athena.execution.backend import ExecutionBackend

# 输出上限（字符）与头部占比：留头看命令回显，留尾看 traceback/最终指标。
MAX_OUTPUT_CHARS = 60_000
OUTPUT_HEAD_SHARE = 1 / 3


class BoundedOutput:
    """流式累积一条输出流，只保留头尾两段，中间省略。"""

    __slots__ = (
        "_limit",
        "_head_budget",
        "_tail_budget",
        "_head",
        "_head_len",
        "_tail",
        "_tail_len",
        "_total",
        "_full",
    )

    def __init__(self, limit: int | None = None, *, keep_full: bool = False) -> None:
        # 有意在这里读模块全局而不是写进默认参数：默认参数在 def 时求值，
        # 测试里 monkeypatch MAX_OUTPUT_CHARS 就不会生效。
        self._limit = MAX_OUTPUT_CHARS if limit is None else limit
        self._head_budget = max(1, int(self._limit * OUTPUT_HEAD_SHARE))
        self._tail_budget = max(1, self._limit - self._head_budget)
        self._head: list[str] = []
        self._head_len = 0
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._total = 0
        self._full: list[str] | None = [] if keep_full else None

    def append(self, text: str) -> None:
        """收下一块已解码的文本。"""
        if not text:
            return
        self._total += len(text)
        if self._full is not None:
            self._full.append(text)
        if self._head_len < self._head_budget:
            room = self._head_budget - self._head_len
            self._head.append(text[:room])
            self._head_len += min(room, len(text))
            text = text[room:]
            if not text:
                return
        self._tail.append(text)
        self._tail_len += len(text)
        while self._tail and self._tail_len - len(self._tail[0]) >= self._tail_budget:
            self._tail_len -= len(self._tail.popleft())

    @property
    def total(self) -> int:
        """这条流一共产生了多少字符（不受上限影响）。"""
        return self._total

    @property
    def truncated(self) -> bool:
        """是否真的省略了内容。"""
        return self._total > self._head_budget + self._tail_budget

    def full_text(self) -> str:
        """完整文本；``keep_full=False`` 时退化为可见部分。"""
        if self._full is not None:
            return "".join(self._full)
        return self.text()

    def text(self, output_ref: str | None = None) -> str:
        """模型可见的文本：头 + 省略标记 + 尾。"""
        if not self.truncated:
            return "".join(self._head) + "".join(self._tail)
        elided = self._total - self._head_budget - self._tail_budget
        where = f"; full output at {output_ref}" if output_ref else ""
        mark = f"\n...[{elided} chars elided{where}]...\n"
        tail = "".join(self._tail)[-self._tail_budget :]
        return "".join(self._head) + mark + tail


# 子进程保留的宿主环境变量白名单；PATH 类保证 PowerShell 能解析可执行文件。
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
        # uv/pip/git 联网、私有索引与代理所需。
        "UV_INDEX_URL",
        "UV_CACHE_DIR",
        "UV_LINK_MODE",
        "UV_NO_CACHE",
        "UV_PYTHON",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
)

# Windows shell 探测顺序：PowerShell 7 → 5.1 → cmd；绝不自动路由到 WSL。
_WIN_SHELLS: tuple[tuple[str, list[str]], ...] = (
    (
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        ["-NoProfile", "-NonInteractive", "-Command"],
    ),
    (
        r"C:\Program Files\PowerShell\7-preview\pwsh.exe",
        ["-NoProfile", "-NonInteractive", "-Command"],
    ),
    # 裸名走 PATH。Microsoft Store / winget / scoop 装的 pwsh 7 不在上面两个
    # 固定目录下，只认绝对路径就会漏掉它、掉到 5.1；而 5.1 的 Get-Content 按
    # 系统 ANSI 代码页读文件，中文 Windows 上会把 UTF-8 文本读成乱码直接喂给
    # agent（2026-08-30 实测：README.md 被读成「Athena 鐨勪换鍔℃暟鎹洰褰」）。
    ("pwsh", ["-NoProfile", "-NonInteractive", "-Command"]),
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
class CommandRequest:
    """一条命令的不可变执行请求。

    Defaults match the historical ``ExecutionRuntime.run`` behaviour: a shell
    command, 120-second timeout, ``workspace_root`` as the working directory,
    and no event emitter unless one is supplied.
    """

    command: str | None = None
    argv: list[str] | None = None
    timeout_s: int = 120
    workdir: str | Path | None = None
    emit: EmitEvent | None = None
    # Internal plumbing: populated by ExecutionRuntime from
    # ExecutionContext.predict_features before the backend sees the request.
    # Kept here so SSH can reject it via the request object instead of a
    # global mutable protocol method.
    predict_features: Path | None = None
    data_csv: Path | None = None


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """一次命令执行的上下文：三个根路径 + 可选 experiment_id + 可选预测目标。"""

    project_root: Path
    workspace_root: Path
    environment_root: Path
    experiment_id: str | None = None
    predict_features: Path | None = None
    data_csv: Path | None = None


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


_ATHENA_WRITE_MARKERS = re.compile(
    r"(set-content|add-content|out-file|new-item|remove-item|move-item|"
    r"copy-item|rename-item|mkdir|rmdir|\bdel\b|\brm\b|\berase\b|"
    r"writealltext|writeallbytes|\.write_text\s*\(|"
    r"open\s*\([^)]*['\"][wa]['\"]|\|\s*out-file)",
    re.IGNORECASE,
)


def _reject_athena_shell_write(
    command: str, workdir: str | None, workspace_root: Path
) -> None:
    """框架私有目录只读：shell 不得在 ``.athena/`` 内工作或对其执行写操作。"""
    if workdir:
        workdir_path = Path(workdir)
        if not workdir_path.is_absolute():
            workdir_path = workspace_root / workdir_path
        if ".athena" in workdir_path.resolve().parts:
            raise ValueError(
                ".athena/ is owned by the Athena runtime and is read-only for "
                "agents; run shell commands from your workspace instead."
            )
    if ".athena" in command and _ATHENA_WRITE_MARKERS.search(command):
        raise ValueError(
            ".athena/ is owned by the Athena runtime and is read-only for agents; "
            "inspect it with read_file/Get-Content, never write it."
        )


class EnvironmentManager:
    """平台/shell/工具探测、子进程环境构造与运行时摘要。"""

    def __init__(
        self,
        *,
        project_root: str | Path,
        environment_root: str | Path,
        data_root: str | Path | None = None,
        host: dict[str, str] | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._environment_root = Path(environment_root)
        self._data_root = Path(data_root) if data_root is not None else None
        self._host = host if host is not None else os.environ
        self._versions: dict[str, str] = {}
        self._needs_repair: str | None = None

    def env_ref(self, name: str) -> str:
        """按当前 shell 的语法引用一个环境变量。"""
        try:
            shell = Path(self.shell_parts()[0]).name.lower()
        except RuntimeError:
            return f"${name}"
        if "powershell" in shell or "pwsh" in shell:
            return f"$env:{name}"
        if shell == "cmd.exe":
            return f"%{name}%"
        return f"${name}"

    @property
    def os_name(self) -> str:
        """平台显示名（Windows / Linux）。"""
        return "Windows" if os.name == "nt" else "Linux"

    def shell_parts(self) -> tuple[str, list[str]]:
        """返回 ``(shell 绝对路径, 启动参数)``。

        候选项可以是绝对路径（查文件是否存在）或裸名（查 PATH）。原先 Windows
        分支只查绝对路径，装在非标准目录的 pwsh 7 一律探测不到。
        """
        pool = _WIN_SHELLS if os.name == "nt" else _POSIX_SHELLS
        for shell, args in pool:
            if Path(shell).is_absolute():
                if Path(shell).is_file():
                    return shell, args
                continue
            found = shutil.which(shell)
            if found:
                return found, args
        raise RuntimeError(f"no shell found on {self.os_name}")

    def build_env(
        self,
        workspace_root: Path | None = None,
        *,
        predict_features: str | Path | None = None,
        data_csv: str | Path | None = None,
    ) -> dict[str, str]:
        """构造子进程环境：白名单宿主变量 + 环境根/workspace venv 前置 PATH + UTF-8。

        ``predict_features`` is per-call, not stored on the manager.  A caller
        that wants this command to see ``ATHENA_PREDICT_FEATURES`` passes the
        target from its immutable ``ExecutionContext`` / ``CommandRequest``.
        """
        env = {
            key.upper(): value
            for key, value in self._host.items()
            if key.upper() in _HOST_VARS
        }
        bindirs: list[str] = []
        for root in (self._environment_root, workspace_root):
            if root is None:
                continue
            bindir = Path(root) / ".venv" / ("Scripts" if os.name == "nt" else "bin")
            if bindir.is_dir():
                bindirs.append(str(bindir))
        entries: list[str] = bindirs
        entries.extend(p for p in env.get("PATH", "").split(os.pathsep) if p)
        env["PATH"] = os.pathsep.join(dict.fromkeys(entries))
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["ATHENA_ENV_ROOT"] = str(self._environment_root)
        if self._data_root is not None:
            env["ATHENA_DATA_ROOT"] = str(self._data_root)
        if predict_features is not None:
            env["ATHENA_PREDICT_FEATURES"] = str(Path(predict_features))
        if data_csv is not None:
            env["ATHENA_DATA_CSV"] = str(Path(data_csv))
        return env

    def tool_versions(self) -> dict[str, str]:
        """探测 python/uv/git 版本（带缓存；不可用时标记 missing）。"""
        for name in ("python", "uv", "git"):
            if name not in self._versions:
                self._versions[name] = self._probe(name)
        return self._versions

    def environment_hash(self) -> str:
        """环境快照哈希：pyproject.toml + uv.lock + python 版本（刷新失效判定）。"""
        digest = hashlib.sha256()
        for name in ("pyproject.toml", "uv.lock"):
            path = self._environment_root / name
            if path.is_file():
                digest.update(path.read_bytes())
        digest.update(self.tool_versions().get("python", "unknown").encode())
        return digest.hexdigest()[:16]

    def ensure_project(self) -> None:
        """确保环境根有可被 ``uv add --project`` 使用的 pyproject.toml（首次初始化）。"""
        pyproject = self._environment_root / "pyproject.toml"
        if pyproject.is_file():
            return
        pyproject.parent.mkdir(parents=True, exist_ok=True)
        pyproject.write_text(
            "[project]\n"
            'name = "athena-environment"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = []\n",
            encoding="utf-8",
        )

    def sync(self, *, frozen: bool = False) -> dict[str, object]:
        """在环境根运行 ``uv sync``（frozen 时 ``--frozen``）；返回 ``{ready, error?}``。"""
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
        """模型可见的简洁运行时块（注入 system prompt；失败给出可行动错误）。"""
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
        shell_line = f"- Shell: {shell_name}"
        if shell_name == "powershell.exe":
            shell_line += ' (chain with ";" not "&&")'
        env_root = self.env_ref("ATHENA_ENV_ROOT")
        lines = [
            "Runtime:",
            f"- OS: {self.os_name}",
            shell_line,
            f"- Workspace: {workspace_root}",
            f"- Python: {python}, {state}",
        ]
        if self._data_root is not None:
            data_ref = self.env_ref("ATHENA_DATA_ROOT")
            lines.append(
                f'- Dataset directory: "{data_ref}" in the shell, '
                'os.environ["ATHENA_DATA_ROOT"] in Python. '
                "Never hardcode an absolute dataset path: it differs per machine."
            )
        lines.append(
            f'- Add dependencies with: uv add --project "{env_root}" <package>'
        )
        return "\n".join(lines)

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


def _stream_fallback_encoding() -> str:
    """子进程输出非合法 UTF-8 时的回退解码编码。"""
    if os.name != "nt":
        return "utf-8"
    return locale.getpreferredencoding(False) or "utf-8"


class _StreamDecoder:
    """把一条子进程输出流按 UTF-8 或系统代码页增量解码。"""

    def __init__(self, fallback_encoding: str) -> None:
        self._fallback = fallback_encoding
        self._buf = bytearray()  # 编码未判定期间缓冲的原始字节
        self._decoder = None  # 编码判定后启用的增量解码器

    def decode(self, raw: bytes, final: bool = False) -> str:
        """增量解码一块字节；编码尚未判定时先缓冲并返回空串。"""
        if self._decoder is not None:
            return self._decoder.decode(raw, final=final)
        self._buf.extend(raw)
        if not final and self._buf.isascii():
            # 纯 ASCII 直接放行，不要攒着等编码判定。
            #
            # 放行是安全的：ASCII 字节在 UTF-8 和各 ANSI 代码页下含义相同，且不可能
            # 是某个多字节字符的后续字节——GBK 的尾字节确实可以落在 0x40-0x7E，但那
            # 需要一个 >=0x80 的前导字节，而缓冲区全是 ASCII 就说明没有前导字节。
            # 编码判定照旧推迟到第一批非 ASCII 字节到达，所以“别把后续 GBK 误判成
            # UTF-8”这条保证不变。
            #
            # 攒着的代价是致命的：``BoundedOutput`` 的 60000 字符上限在本类**下游**，
            # 这里返回空串就等于绕过它，整条输出会原封不动堆在内存里直到进程退出。
            # 2026-08-30 实测：shell 从 PowerShell 5.1 换到 7 之后，``dir`` 的表头从
            # 中文“目录:”变成纯英文 "Directory:"，这条分支不再被非 ASCII 打断，
            # 主进程工作集冲到 20 GB（物理内存共 31.5 GB）。5.1 时代是靠输出里恰好
            # 有中文才没炸——一个谁也没设计过的安全阀。
            text = self._buf.decode("ascii")
            self._buf.clear()
            return text
        encoding = self._classify(final)
        if encoding is None:
            return ""
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        pending = bytes(self._buf)
        self._buf.clear()
        return self._decoder.decode(pending, final=final)

    def _classify(self, final: bool) -> str | None:
        data = bytes(self._buf)
        try:
            data.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                return None if not final else "utf-8"
            return self._fallback
        return "utf-8"


class CommandExecutor:
    """在选定 shell 中执行命令；流式事件、超时/取消、进程树终止与有界输出。"""

    def __init__(
        self,
        *,
        env: dict[str, str],
        persist: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._env = env
        self._persist = persist
        self._fallback_encoding = _stream_fallback_encoding()

    def _spawn_flags(self) -> tuple[int, bool]:
        """Windows 用独立进程组（taskkill 可杀整树）；POSIX 用新会话。"""
        if os.name == "nt":
            return subprocess.CREATE_NEW_PROCESS_GROUP, False
        return 0, True

    @staticmethod
    def _utf8_command(shell: str, command: str) -> str:
        """PowerShell 显式设 UTF-8 输出编码并透传最后命令退出码。"""
        name = Path(shell).name.lower()
        if "powershell" in name or "pwsh" in name:
            # 用换行分隔 exit：避免 `; exit ...` 被末尾注释吞掉。
            return (
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "$OutputEncoding=[Text.Encoding]::UTF8;"
                # 兜底：上面两行只管**写**出去的编码。PowerShell 5.1 读文件
                # 默认用系统 ANSI，UTF-8 文本会在读入时就变成乱码，再怎么
                # 正确地写出去也没用。7 默认就是 UTF-8，这行对它无害。
                "$PSDefaultParameterValues['*:Encoding']='utf8';"
                + command
                + "\nexit $LASTEXITCODE"
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

    def _resolve_executable(self, argv: list[str]) -> list[str]:
        """把 argv[0] 的裸可执行名解析为环境 PATH 里的绝对路径。"""
        resolved = shutil.which(argv[0], path=self._env.get("PATH", ""))
        return [resolved, *argv[1:]] if resolved else argv

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
        """执行命令并返回紧凑结果；命令事件经 ``emit`` 流式推送。"""
        _validate_command_input(command, argv)
        flags, new_session = self._spawn_flags()
        display = " ".join(argv) if argv is not None else command
        await _dispatch(emit, "command/started", "exec:run", {"command": display})
        try:
            if argv is not None:
                argv = self._resolve_executable(argv)
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
            error = "command_not_found" if argv is not None else "shell_not_found"
            return CommandResult(
                ok=False, stdout="", stderr="", exit_code=127, error=error
            )

        keep_full = self._persist is not None
        out = BoundedOutput(keep_full=keep_full)
        err = BoundedOutput(keep_full=keep_full)
        digest = hashlib.sha256()

        async def pump(
            stream: asyncio.StreamReader, kind: str, sink: BoundedOutput
        ) -> None:
            """读取流式输出：边 hash 完整输出边累积有界头尾。"""
            decoder = _StreamDecoder(self._fallback_encoding)

            async def emit_decoded(text: str) -> None:
                """把解码好的一段推给上层，同时进有界缓冲。"""
                if not text:
                    return
                await _dispatch(emit, kind, "exec:out", {"delta": text})
                sink.append(text)

            async for raw in stream:
                digest.update(raw)
                await emit_decoded(decoder.decode(raw))
            await emit_decoded(decoder.decode(b"", final=True))

        readers = [
            asyncio.create_task(pump(proc.stdout, "command/stdout", out)),
            asyncio.create_task(pump(proc.stderr, "command/stderr", err)),
        ]
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            self._terminate(proc)
            # drain 也要有界：孙进程可能脱离进程组仍霸占管道。
            try:
                await asyncio.wait_for(
                    asyncio.gather(*readers, return_exceptions=True), timeout=5
                )
            except asyncio.TimeoutError:
                pass
            return CommandResult(
                ok=False,
                stdout=out.text(),
                stderr=err.text(),
                exit_code=-1,
                error="timeout",
            )
        except asyncio.CancelledError:
            self._terminate(proc)
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        await asyncio.gather(*readers)

        truncated = out.truncated or err.truncated
        if self._persist is not None and (returncode != 0 or truncated):
            full_text = "\n".join(
                part for part in (out.full_text(), err.full_text()) if part
            )
            output_ref = await self._persist(full_text)
        elif truncated:
            output_ref = "sha256:" + digest.hexdigest()
        else:
            output_ref = None
        result = CommandResult(
            ok=returncode == 0,
            stdout=out.text(output_ref),
            stderr=err.text(output_ref),
            exit_code=returncode or 0,
            truncated=truncated,
            output_ref=output_ref,
        )
        await _dispatch(emit, "command/completed", "exec:run", result.to_dict())
        return result


class ExecutionRuntime:
    """共享执行运行时门面：选择后端 → 执行命令 → 流式事件 → 紧凑结果。"""

    def __init__(
        self,
        *,
        project_root: str | Path,
        environment_root: str | Path | None = None,
        data_root: str | Path | None = None,
        store: ArtifactStore | None = None,
        backend: "ExecutionBackend | None" = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._environment_root = (
            Path(environment_root)
            if environment_root is not None
            else self._project_root
        )
        self._data_root = Path(data_root) if data_root is not None else None
        if backend is None:
            # backend 需要本模块的 EnvironmentManager/CommandExecutor
            from athena.execution.backend import LocalBackend  # 延迟导入避免循环依赖

            backend = LocalBackend(
                project_root=self._project_root,
                environment_root=self._environment_root,
                data_root=self._data_root,
                store=store,
            )
        self._backend = backend

    @property
    def project_root(self) -> Path:
        """研究项目根（非 Athena 源码树）。"""
        return self._project_root

    @property
    def environment_root(self) -> Path:
        """环境根（含 pyproject.toml / uv.lock / .venv）。"""
        return self._environment_root

    @property
    def data_root(self) -> Path | None:
        """数据集根目录；未配置时为 None（此时不注入 ATHENA_DATA_ROOT）。"""
        return self._data_root

    def shell_command_tool(self, workspace_root: str | Path) -> BaseTool:
        """构造模型可见的 shell_command 工具（cwd 默认 workspace）。"""
        return _ShellCommandTool(self, Path(workspace_root))

    @property
    def backend(self) -> "ExecutionBackend":
        """当前执行后端（命令真正落在哪台机器上）。"""
        return self._backend

    def runtime_summary(self, workspace_root: str | Path) -> str:
        """注入 system prompt 的简洁运行时块——来自真正执行命令的那台机器。"""
        return self._backend.describe(Path(workspace_root))

    def env_ref(self, name: str) -> str:
        """按执行机器的 shell 语法引用一个环境变量。"""
        return self._backend.env_ref(name)

    def ensure_environment(self) -> None:
        """确保共享环境根已初始化（含可被 uv 使用的 pyproject.toml）。"""
        self._backend.ensure_environment()

    async def run(
        self,
        context: ExecutionContext,
        request: CommandRequest,
    ) -> CommandResult:
        """直接执行 API（Supervisor/Service/Runner 复用同一语义）。

        ``predict_features`` travels on the immutable execution context and is
        materialised onto the per-command request before it reaches the backend.
        """
        _validate_command_input(request.command, request.argv)
        cwd = (
            Path(request.workdir)
            if request.workdir is not None
            else context.workspace_root
        )
        backend_request = replace(
            request,
            predict_features=(
                context.predict_features
                if context.predict_features is not None
                else request.predict_features
            ),
            data_csv=(
                context.data_csv if context.data_csv is not None else request.data_csv
            ),
        )
        return await self._backend.run(
            workspace_root=context.workspace_root,
            request=backend_request,
        )

    async def collect_outputs(self, subdirs: tuple[str, ...]) -> None:
        """把 manifest 声明的产出取到本地，供本地评估器打分。"""
        await self._backend.collect_outputs(tuple(subdirs))

    async def aclose(self) -> None:
        """释放后端资源（本地无事可做；远程要关掉常驻通道）。"""
        await self._backend.aclose()


class _ShellCommandTool(BaseTool):
    """模型可见的 shell_command 工具（唯一命令工具，替换 bash/pwsh）。"""

    spec = ToolSpec(
        name="shell_command",
        description=(
            "Run a shell command in the workspace and return stdout/stderr/exit_code. "
            "A nonzero exit is a normal result; read stderr, fix the command, "
            "and retry in the same turn. "
            "When output is long (e.g. a huge error list or registry dump), do not "
            "read it all; pipe the command through a text search first, e.g. "
            "`cmd 2>&1 | grep keyword`, `cmd 2>&1 | findstr keyword`, or "
            "`cmd 2>&1 | Select-String keyword`."
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
        command = str(input["command"])
        workdir = input.get("workdir")
        _reject_athena_shell_write(command, workdir, self._workspace_root)
        context = ExecutionContext(
            project_root=self._runtime.project_root,
            workspace_root=self._workspace_root,
            environment_root=self._runtime.environment_root,
        )
        result = await self._runtime.run(
            context,
            CommandRequest(
                command=command,
                timeout_s=int(input.get("timeout_s", 120)),
                workdir=workdir,
                emit=ctx.emit,
            ),
        )
        return result.to_dict()

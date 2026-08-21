"""共享执行运行时：统一 ``shell_command`` 工具与直接执行 API。

对齐 codex_docs/2026-08-11-shared-execution-runtime-design.md 首版范围。
``ExecutionRuntime`` 是唯一入口：选择环境 → 执行命令 → 流式事件 → 紧凑结果。
``EnvironmentManager`` 负责平台/shell/python/uv/git 探测与子进程环境构造；
``CommandExecutor`` 负责起进程、流式读取、超时/取消与有界输出。本模块不含任何
EDA/训练/评估业务决策；安全隔离与远程执行是显式未来工作。
"""

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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from athena.core.contracts import ArtifactStore
from athena.core.tool import BaseTool
from athena.core.tool_types import EmitEvent, ToolContext, ToolSpec

if TYPE_CHECKING:
    from athena.execution.backend import ExecutionBackend

# 有界输出上限（字符）：超出后截断并记录完整输出 hash，避免无限累积。
MAX_OUTPUT_CHARS = 60_000

# 模型可见输出里头部占的比例。留头是为了知道"跑的是什么"（命令回显、配置、前几个
# epoch）；留尾是因为**失败现场在最后**——traceback、最终指标、OOM 全在尾巴上。
# 尾部拿大头。
OUTPUT_HEAD_SHARE = 1 / 3


class BoundedOutput:
    """流式累积一条输出流，只保留头尾两段，中间省略。

    为什么不是"保头砍尾"（本类出现之前的做法）：一次训练打几千行日志，前面是配置
    回显，最后才是 traceback / 最终指标 / OOM。砍尾恰好把唯一有用的那一段删掉，
    而且流读过就没了——不像文件还能再读一次。

    对话历史那一层（``tool_types.truncate_text``）本来就是留头尾的，但它拿到的
    已经是这里砍完的结果，所以那一层的头尾保留在这里被架空了。两层现在同口径。

    ``keep_full`` 时另留一份完整文本落成证据 artifact。这是 ``truncated: true``
    加 ``output_ref`` 这个契约的兑现方式：那个 ref 承诺"完整输出在这里"，
    就必须真的完整——否则 agent 拿着 ref 去查，查到的还是被砍过的那份。
    """

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
        # 只丢"丢掉也还够尾部预算"的那些块；内存上界是预算加一块。
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
        """模型可见的文本：头 + 省略标记 + 尾。

        标记必须留在正文里，而不是只在 ``truncated`` 字段上：agent 读到的是这段
        文字，它得当场知道中间断了、断了多少、去哪儿取全的。
        """
        if not self.truncated:
            return "".join(self._head) + "".join(self._tail)
        elided = self._total - self._head_budget - self._tail_budget
        where = f"; full output at {output_ref}" if output_ref else ""
        mark = f"\n...[{elided} chars elided{where}]...\n"
        tail = "".join(self._tail)[-self._tail_budget :]
        return "".join(self._head) + mark + tail


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
        # uv/pip/git 联网、私有索引、自签证书与代理所需；缺失会让他们在代理/镜像
        # 环境下静默失败。
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
    """框架私有目录只读：shell 不得在 ``.athena/`` 内工作或对其执行写操作。

    命令字符串只能做启发式识别（LLM 可换写法绕过），真正的强约束由
    ``write_file`` 工具与 prompt 约束共同承担。
    """
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
        """按当前 shell 的语法引用一个环境变量。

        实测过的坑：注入子进程的是**环境**变量，而 PowerShell 里 ``$FOO`` 取的是
        PowerShell 变量，未定义就静默展开成空串——``uv add --project "$ATHENA_ENV_ROOT"``
        在 Windows 上等价于 ``--project ""``。必须写 ``$env:FOO``。cmd 用 ``%FOO%``。
        探测不到 shell 时退回 POSIX 写法（与 runtime_summary 的错误分支一致）。
        """
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

        Windows 顺序 PowerShell 7 → 5.1 → cmd，Linux 顺序 bash → sh。
        探测失败抛 ``RuntimeError``（由调用方决定降级或报错）。
        """
        pool = _WIN_SHELLS if os.name == "nt" else _POSIX_SHELLS
        for shell, args in pool:
            if os.name != "nt":
                found = shutil.which(shell)
                if found:
                    return found, args
            elif Path(shell).is_file():
                return shell, args
        raise RuntimeError(f"no shell found on {self.os_name}")

    def build_env(self, workspace_root: Path | None = None) -> dict[str, str]:
        """构造子进程环境：白名单宿主变量 + 环境根/workspace venv 前置 PATH + UTF-8。

        ``ATHENA_ENV_ROOT`` 指向含 ``pyproject.toml``/``uv.lock`` 的环境根，
        供 agent 用 ``uv add --project "$ATHENA_ENV_ROOT"`` 动态加依赖。环境根
        venv 优先，其次 workspace 本地 venv（agent 用 ``uv sync`` 就地装的依赖
        也能被裸 ``python`` 解析到），避免 ``import numpy`` 类 ModuleNotFoundError。
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
        # 数据集根目录。agent 生成的脚本必须靠它定位数据，而不是把绝对路径写死：
        # 写死的路径换台机器（尤其换到 Linux GPU 机）第一行 read_csv 就炸。
        if self._data_root is not None:
            env["ATHENA_DATA_ROOT"] = str(self._data_root)
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

    def ensure_project(self) -> None:
        """确保环境根有可被 ``uv add --project`` 使用的 pyproject.toml（首次初始化）。

        PREPARE agent 依赖 ``uv add --project "$ATHENA_ENV_ROOT" <package>`` 装依赖；
        空环境根没有 pyproject.toml 会让该命令报 ``No pyproject.toml found``，agent
        因而退到 workspace 本地 venv，确定性 runner 的裸 ``python`` 解析到无依赖解释器
        （``train_model.py`` 在 import numpy 处 ModuleNotFoundError 死循环）。幂等。
        """
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
        # PowerShell 5.1（powershell.exe）不支持 ``&&``；给 agent 明示避免其生成
        # ``cmd1 && cmd2`` 触发解析错误、浪费轮次（pwsh 7 支持 &&，无需提示）。
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
    """子进程输出非合法 UTF-8 时的回退解码编码。

    Windows 上 PowerShell 与原生控制台程序按系统代码页输出（中文系统为
    cp936/GBK），而 Python 子进程被 ``build_env`` 强制为 UTF-8，因此单一
    编码无法覆盖全部输出。返回系统代码页作为回退；非 Windows 平台 UTF-8
    即常态，回退为 utf-8（等价于不改变现有行为）。
    """
    if os.name != "nt":
        return "utf-8"
    return locale.getpreferredencoding(False) or "utf-8"


class _StreamDecoder:
    """把一条子进程输出流按 UTF-8 或系统代码页增量解码。

    一次命令可能混出两种编码：Python 子进程被 ``PYTHONIOENCODING``/``PYTHONUTF8``
    强制 UTF-8，而 PowerShell 自身 stdout 与解析错误 stderr 走控制台代码页
    （中文 Windows 为 GBK）。两者共享 ASCII，故先按字节缓冲，直到出现首个
    非 ASCII 字节才判定：字节序列合法 UTF-8 则选 utf-8，否则选回退编码。
    单条流内真正混合编码（罕见）时少数部分退化为 U+FFFD，与旧行为一致。
    """

    def __init__(self, fallback_encoding: str) -> None:
        self._fallback = fallback_encoding
        self._buf = bytearray()  # 编码未判定期间缓冲的原始字节
        self._decoder = None  # 编码判定后启用的增量解码器

    def decode(self, raw: bytes, final: bool = False) -> str:
        """增量解码一块字节；编码尚未判定时先缓冲并返回空串。"""
        if self._decoder is not None:
            return self._decoder.decode(raw, final=final)
        self._buf.extend(raw)
        # 纯 ASCII 在任意候选编码下都相同：保持缓冲、暂不判定，避免把后续
        # GBK 误判为 UTF-8（空/纯 ASCII 也走这里，开销极小）。
        if not final and self._buf.isascii():
            return ""
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
                # 多字节 UTF-8 字符跨 pipe 分块被截断：等待更多字节；final 时
                # 流以截断 UTF-8 收尾，仍选 utf-8 并靠 errors="replace" 兜底。
                return None if not final else "utf-8"
            # 真正非法的 UTF-8（如 GBK/cp936）→ 回退到系统代码页。
            return self._fallback
        return "utf-8"


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
        self._fallback_encoding = _stream_fallback_encoding()

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
            # 用换行而非 `;` 分隔 exit：command 以 `# 注释` 结尾时，`; exit ...`
            # 会被吞进注释导致真实非零退出码被误判成 0。
            return (
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "$OutputEncoding=[Text.Encoding]::UTF8;"
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
        """把 argv[0] 的裸可执行名解析为环境 PATH 里的绝对路径。

        Windows ``CreateProcess`` 对无路径、无扩展名的可执行名（如裸 ``python``）
        按「父进程目录 → 当前目录 → System → PATH」搜索，未必落到 ``build_env``
        前置的 venv，使 manifest 的裸 ``python`` 解析到无依赖解释器
        （``ModuleNotFoundError`` 死循环）。这里用环境 PATH 显式解析，绕开
        CreateProcess 的搜索歧义；已含路径或未找到时原样返回，交给
        ``create_subprocess_exec`` 处理。
        """
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
            # 可执行文件/shell 不存在 → 不穿透，给可行动错误
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
            """读取流式输出：边 hash 完整输出边累积有界头尾。

            用 ``_StreamDecoder`` 跨 chunk 解码：优先 UTF-8，遇到 GBK 等系统
            代码页字节时回退解码，既避免多字节字符在 chunk 边界被撕裂成 U+FFFD，
            也修正中文 Windows 上 PowerShell/原生命令输出的 mojibake。
            """
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
            self._terminate(proc)  # 超时 → 终止整个进程树
            # drain 也要有界：孙进程可能脱离进程组仍霸占管道，导致 gather 永久挂起。
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
            self._terminate(proc)  # 取消 turn → 终止整个进程树后传播
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        await asyncio.gather(*readers)

        truncated = out.truncated or err.truncated
        if self._persist is not None and (returncode != 0 or truncated):
            # 持久化失败/截断命令的完整日志为证据 artifact（design §Persistence Policy）
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
            # ref 先算再渲染：省略标记要把"去哪儿取全的"写进正文。
            stdout=out.text(output_ref),
            stderr=err.text(output_ref),
            exit_code=returncode or 0,
            truncated=truncated,
            output_ref=output_ref,
        )
        await _dispatch(emit, "command/completed", "exec:run", result.to_dict())
        return result


class ExecutionRuntime:
    """共享执行运行时门面：选择后端 → 执行命令 → 流式事件 → 紧凑结果。

    命令跑在哪台机器上由 ``backend`` 决定，门面本身不知道——``backend.py`` 的
    ``ExecutionBackend`` 就是那道界线。缺省是 ``LocalBackend``（控制节点自己），
    行为与引入这条缝之前逐字相同。

    扩展点（均不静默部分实现）：
    - TODO(execution-security): 强制文件系统/进程/网络/命令策略。
    - TODO(container-executor): 在强隔离容器中执行命令（DockerLauncher）。
    - TODO(environment-lock): PREPARE 并发时序列化依赖变更。
    """

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
        return await self._backend.run(
            command=command,
            argv=argv,
            workspace_root=context.workspace_root,
            workdir=cwd,
            timeout_s=timeout_s,
            emit=emit,
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
            command,
            timeout_s=int(input.get("timeout_s", 120)),
            workdir=workdir,
            emit=ctx.emit,
        )
        return result.to_dict()

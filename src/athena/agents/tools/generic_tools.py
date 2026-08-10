"""通用文件/命令工具集（对齐 Pi：read_file / write_file / bash / pwsh）。

沙箱约定：``read_file``/``write_file`` 限定 workspace 内（路径逃逸防护）；
``bash``/``pwsh`` 以 workspace 为 cwd 执行。Python 脚本经 ``bash("python x.py")``
覆盖。四个工具由 ``generic_tool_registry`` 用闭包捕获 workspace，一行 ``@tool``
定义。
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

from athena.core.tool import ToolRegistry, tool

# 保留宿主 PATH / home / 临时目录等运行环境变量；过滤凭据类（API key 等）。
# 缺 USERPROFILE/HOME/HOMEDRIVE/HOMEPATH 时 conda/python 的 ``expanduser()``
# 无法确定 home 目录（报 "Could not determine home directory"），pwsh/bash
# 启动时加载用户 profile 里的 conda init 会失败并弹窗。
_HostAllow = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
)


def _workspace_path(root: Path, path: str) -> Path:
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


def _shell_env() -> dict[str, str]:
    """构造子进程环境：保留宿主 PATH 并把当前解释器 Scripts 前置。

    原型沿用宿主环境（supervisor_design §2.6 strong_isolation=false）：LLM 生成
    脚本用 ``python analysis.py`` 运行，若 PATH 缺当前 venv 的 ``Scripts`` 目录，
    ``python`` 会解析失败 → ReAct 循环反复修脚本而无法收尾。这里确保 ``python``/
    ``uv`` 始终可解析。仍过滤凭据类变量，避免脚本读取宿主机密。
    """
    env = {k: v for k, v in os.environ.items() if k in _HostAllow}
    scripts = Path(sys.executable).resolve().parent
    if "PATH" in env and str(scripts) not in env["PATH"]:
        env["PATH"] = str(scripts) + os.pathsep + env["PATH"]
    return env


# 可经环境变量显式指定 shell 路径（对齐 Pi 的 shell_path 设置；优先于探测）。
_ENV_SHELL = {"bash": "ATHENA_BASH_PATH", "pwsh": "ATHENA_PWSH_PATH"}


def _is_windows() -> bool:
    return os.name == "nt"


def _resolve_shell(name: str, env: dict[str, str] | None = None) -> str:
    """返回确定的 shell 绝对路径（跨平台，参考 Codex/Pi）。

    优先级：环境变量覆盖（``ATHENA_BASH_PATH``/``ATHENA_PWSH_PATH``）→
    平台已知路径 → ``shutil.which``。Windows 下 ``bash`` 显式走 Git Bash，
    避免 ``System32\\bash.exe``（WSL launcher）被选中；``pwsh`` 优先
    PowerShell 7/5.1。Unix 下 ``bash`` 用系统 bash（回退 ``sh``）；
    ``pwsh`` 缺失时返回 None（由调用方降级为 bash），保证任何机器可用。
    """
    if env is None:
        env = os.environ
    override = env.get(_ENV_SHELL[name])
    if override:
        path = Path(override)
        if path.is_file():
            return str(path)
        raise RuntimeError(f"configured {name} path not found: {override}")

    if _is_windows():
        candidates = {
            "bash": [
                r"C:\Program Files\Git\usr\bin\bash.exe",
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files\Git\mingw64\bin\bash.exe",
            ],
            "pwsh": [
                r"C:\Program Files\PowerShell\7\pwsh.exe",
                r"C:\Program Files\PowerShell\7-preview\pwsh.exe",
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ],
        }
        for candidate in candidates.get(name, []):
            if Path(candidate).is_file():
                return candidate

    found = shutil.which(name)
    if found:
        return found
    if name == "bash":
        sh_path = shutil.which("sh")
        if sh_path:
            return sh_path
    return None


async def _run_command(
    root: Path, shell: str, args: list[str], command: str, timeout_s: int
) -> dict:
    """执行 shell 命令；``pwsh`` 在无 PowerShell 的平台（如 Unix）降级为 bash。

    降级后参数换成 bash 风格（``--noprofile -c``），保证 ``pwsh`` 工具在
    任何机器可用（对齐 Codex：Unix 统一走 POSIX shell）。
    """
    shell_path = _resolve_shell(shell)
    if shell_path is None:
        if shell == "pwsh":
            shell_path = _resolve_shell("bash")
            args = ["--noprofile", "-c"]
        else:
            raise RuntimeError(f"shell not found: {shell}")
    proc = await asyncio.create_subprocess_exec(
        shell_path,
        *args,
        command,
        cwd=str(root.resolve()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_shell_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"TIMEOUT after {timeout_s}s") from exc
    return {
        "returncode": proc.returncode or 0,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def generic_tool_registry(workspace: Path) -> ToolRegistry:
    """构造对齐 Pi 的最小通用工具集：read_file / write_file / bash / pwsh。"""
    root = workspace.resolve()

    @tool
    async def read_file(
        path: str, start_line: int | None = None, end_line: int | None = None
    ) -> dict:
        """Read a file, optionally within a 1-based line range [start_line, end_line]."""
        path_obj = _workspace_path(root, path)
        if not path_obj.is_file():
            raise FileNotFoundError(path_obj)
        lines = path_obj.read_text(encoding="utf-8").splitlines()
        if start_line is None and end_line is None:
            content = "\n".join(lines)
        else:
            content = "\n".join(lines[(start_line or 1) - 1 : end_line or len(lines)])
        return {"path": str(path_obj), "content": content}

    @tool
    async def write_file(path: str, content: str) -> dict:
        """Create or overwrite a file in the workspace."""
        path_obj = _workspace_path(root, path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(content, encoding="utf-8")
        return {"path": str(path_obj)}

    def _command_tool(shell: str, args: list[str], name: str, description: str):
        @tool(name=name, description=description)
        async def _cmd(command: str, timeout_s: int = 120) -> dict:
            """Run a shell command in the workspace."""
            return await _run_command(root, shell, args, command, timeout_s)

        return _cmd

    reg = ToolRegistry()
    reg.register(read_file)
    reg.register(write_file)
    reg.register(
        _command_tool(
            "bash",
            ["--noprofile", "-c"],
            "bash",
            "Run a bash command in the workspace and return stdout/stderr/returncode",
        )
    )
    reg.register(
        _command_tool(
            "pwsh",
            ["-NoProfile", "-NonInteractive", "-Command"],
            "pwsh",
            "Run a PowerShell command in the workspace and return stdout/stderr/returncode",
        )
    )
    return reg

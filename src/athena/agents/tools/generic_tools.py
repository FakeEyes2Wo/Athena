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

_HostAllow = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


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


def _resolve_shell(name: str) -> str:
    """返回确定的 shell 绝对路径，避免 Windows 下 ``bash`` 被解析为 WSL launcher。

    Windows 的 ``C:\\Windows\\System32\\bash.exe`` 是 WSL 入口（启动后无宿主
    venv/pandas，脚本必然失败）；这里优先 Git Bash 的 ``bash.exe`` 与 Windows
    PowerShell 的 ``powershell.exe``，两者都不存在时回退 shutil.which。
    """
    candidates = {
        "bash": [
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
        ],
        "pwsh": [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            r"C:\Program Files\PowerShell\7\pwsh.exe",
        ],
    }
    for candidate in candidates.get(name, []):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"shell not found: {name}")


async def _run_command(
    root: Path, shell: str, flag: str, command: str, timeout_s: int
) -> dict:
    shell_path = _resolve_shell(shell)
    proc = await asyncio.create_subprocess_exec(
        shell_path,
        flag,
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

    def _command_tool(shell: str, flag: str, name: str, description: str):
        @tool(name=name, description=description)
        async def _cmd(command: str, timeout_s: int = 120) -> dict:
            """Run a shell command in the workspace."""
            return await _run_command(root, shell, flag, command, timeout_s)

        return _cmd

    reg = ToolRegistry()
    reg.register(read_file)
    reg.register(write_file)
    reg.register(
        _command_tool(
            "bash",
            "-c",
            "bash",
            "Run a bash command in the workspace and return stdout/stderr/returncode",
        )
    )
    reg.register(
        _command_tool(
            "pwsh",
            "-Command",
            "pwsh",
            "Run a PowerShell command in the workspace and return stdout/stderr/returncode",
        )
    )
    return reg

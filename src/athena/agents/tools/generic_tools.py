"""通用文件/命令工具集（参考 Codex/Claude）。prompt 驱动 agent 共用。

沙箱约定：``read_file``/``write_file`` 限定 workspace 内（路径逃逸防护）；
``bash``/``pwsh`` 以 workspace 为 cwd 执行。Python 脚本经 ``bash("python x.py")``
覆盖。
"""

import asyncio
from pathlib import Path

from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

_HostAllow = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


def _workspace_path(workspace: Path, path: str) -> Path:
    root = workspace.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


def _shell_env() -> dict[str, str]:
    return {k: v for k, v in __import__("os").environ.items() if k in _HostAllow}


class ReadFileTool(BaseTool):
    """读文件，支持 1 起始行范围（含端点）。"""

    spec = ToolSpec(
        name="read_file",
        description="Read a file, optionally within a 1-based line range [start_line, end_line]",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = _workspace_path(self._workspace, input["path"])
        if not path.is_file():
            return ToolResult(data=None, success=False, error=f"file not found: {path}")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = input.get("start_line")
        end = input.get("end_line")
        if start is None and end is None:
            content = "\n".join(lines)
        else:
            content = "\n".join(lines[(start or 1) - 1 : end or len(lines)])
        return ToolResult(data={"path": str(path), "content": content})


class WriteFileTool(BaseTool):
    """创建/覆盖工作区文件。"""

    spec = ToolSpec(
        name="write_file",
        description="Create or overwrite a file in the workspace",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = _workspace_path(self._workspace, input["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(input["content"], encoding="utf-8")
        return ToolResult(data={"path": str(path)})


class _CommandTool(BaseTool):
    def __init__(
        self, workspace: Path, shell: str, flag: str, name: str, desc: str
    ) -> None:
        self._workspace = Path(workspace)
        self._shell = shell
        self._flag = flag
        self.spec = ToolSpec(
            name=name,
            description=desc,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_s": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
        )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        command = input["command"]
        timeout_s = int(input.get("timeout_s", 120))
        try:
            proc = await asyncio.create_subprocess_exec(
                self._shell,
                self._flag,
                command,
                cwd=str(self._workspace.resolve()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_shell_env(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
            return ToolResult(
                data={
                    "returncode": proc.returncode or 0,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                }
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            return ToolResult(
                data=None, success=False, error=f"TIMEOUT after {timeout_s}s"
            )


class BashTool(_CommandTool):
    """沙箱执行 bash 命令。"""

    def __init__(self, workspace: Path) -> None:
        super().__init__(
            workspace,
            shell="bash",
            flag="-c",
            name="bash",
            desc="Run a bash command in the workspace and return stdout/stderr/returncode",
        )


class PwshTool(_CommandTool):
    """沙箱执行 PowerShell 命令。"""

    def __init__(self, workspace: Path) -> None:
        super().__init__(
            workspace,
            shell="pwsh",
            flag="-Command",
            name="pwsh",
            desc="Run a PowerShell command in the workspace and return stdout/stderr/returncode",
        )


def generic_tool_registry(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadFileTool(workspace))
    reg.register(WriteFileTool(workspace))
    reg.register(BashTool(workspace))
    reg.register(PwshTool(workspace))
    return reg

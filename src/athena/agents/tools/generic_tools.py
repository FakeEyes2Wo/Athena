"""通用文件工具集：read_file / write_file + 统一的 shell_command。

沙箱约定：``read_file``/``write_file`` 限定 workspace 内（路径逃逸防护）；
命令执行统一走 ``ExecutionRuntime.shell_command``（shared-execution-runtime-design）。
``generic_tool_registry`` 需要 ``runtime`` 才注册命令工具——bash/pwsh 已在迁移中
移除（design §Tool Contract），无 runtime 的旧调用只得到文件工具。
"""

from pathlib import Path
from typing import TYPE_CHECKING

from athena.core.tool import ToolRegistry, tool
from athena.core.workspace import resolve_workspace_path

if TYPE_CHECKING:
    from athena.execution.runtime import ExecutionRuntime


def _workspace_path(root: Path, path: str) -> Path:
    try:
        return resolve_workspace_path(root, path)
    except ValueError as exc:
        raise ValueError(
            f"{exc} Use a relative path inside your workspace ({root}); "
            "reach external files via shell_command instead."
        ) from None


def generic_tool_registry(
    workspace: Path, *, runtime: "ExecutionRuntime | None" = None
) -> ToolRegistry:
    """构造通用工具集：read_file / write_file + 可选 shell_command。

    ``runtime`` 提供时注册统一命令工具 ``shell_command``（唯一命令工具，design
    §Tool Contract）；缺省（旧调用/测试）只提供文件工具。
    """
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

    reg = ToolRegistry()
    reg.register(read_file)
    reg.register(write_file)
    if runtime is not None:
        reg.register(runtime.shell_command_tool(root))
    return reg

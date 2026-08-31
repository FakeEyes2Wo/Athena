"""通用文件工具集：read_file / write_file / append_file + 统一的 shell_command。

``append_file`` 是 2026-08-30 补上的：原先只有覆盖式 ``write_file``，一份超过
输出上限的文件就**没有任何办法写出来**——模型只能一次性给出全部内容，被 max_tokens
从中间切断后整个工具调用作废，重试还是同样长度。有了追加，长文件可以拆成若干次
调用逐段落盘。详见 ``docs/Athena_TESS_运行记录.md`` §11。

沙箱约定：``read_file``/``write_file``/``append_file`` 限定 workspace 内（路径逃逸防护）；
命令执行统一走 ``ExecutionRuntime.shell_command``（shared-execution-runtime-design）。
``generic_tool_registry`` 需要 ``runtime`` 才注册命令工具——bash/pwsh 已在迁移中
移除（design §Tool Contract），无 runtime 的旧调用只得到文件工具。
"""

from pathlib import Path
from typing import TYPE_CHECKING

from athena.core.tool import ToolRegistry, tool
from athena.core.workspace import is_framework_owned, resolve_workspace_path

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


def _reject_framework_write(path_obj: Path, root: Path) -> None:
    """框架私有目录只读：agent 不得写 ``.athena/**``（state/tree/logs/artifacts）。

    判定以工作区为界：命名会话的工作区本身就在 ``.athena/`` 下，见 ``.athena``
    就拒会让这些会话连自己的文件都写不了。
    """
    if is_framework_owned(path_obj, root):
        raise ValueError(
            f".athena/ is owned by the Athena runtime and is read-only for agents; "
            f"refusing to write {path_obj}"
        )


def generic_tool_registry(
    workspace: Path, *, runtime: "ExecutionRuntime | None" = None
) -> ToolRegistry:
    """构造通用工具集：read_file / write_file / append_file + 可选 shell_command。

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
        """Create or overwrite a file in the workspace (never under .athena/).

        For a long document, write the first section here and add the rest with
        append_file: one oversized call gets cut off at the output-token limit,
        and a cut-off call writes nothing at all.
        """
        path_obj = _workspace_path(root, path)
        _reject_framework_write(path_obj, root)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        # newline="" 关掉换行翻译：内容按模型给出的样子逐字节落盘。开着的话
        # Windows 上每个 \n 会变成 \r\n，写回的字节数与 content 对不上，
        # append_file 报的进度也就跟着失真。
        with path_obj.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return {"path": str(path_obj), "bytes": len(content.encode("utf-8"))}

    @tool
    async def append_file(path: str, content: str) -> dict:
        """Append to a file in the workspace, creating it if absent.

        Use this to build a long file in several calls instead of one huge
        write_file. Each call should carry one section; the tool reports the
        running size so you can tell how much has landed.
        """
        path_obj = _workspace_path(root, path)
        _reject_framework_write(path_obj)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        with path_obj.open("a", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return {
            "path": str(path_obj),
            "appended_bytes": len(content.encode("utf-8")),
            "total_bytes": path_obj.stat().st_size,
        }

    reg = ToolRegistry()
    reg.register(read_file)
    reg.register(write_file)
    reg.register(append_file)
    if runtime is not None:
        reg.register(runtime.shell_command_tool(root))
    return reg

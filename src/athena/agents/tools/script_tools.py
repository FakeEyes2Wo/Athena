"""Agent 脚本执行与结果提交工具。"""

from pathlib import Path

from athena.code.execution import ExecutionRequest, LocalExperimentRuntime
from athena.core.contracts import ArtifactStore
from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class WriteScriptTool(BaseTool):
    """把脚本内容写入工作区。"""

    spec = ToolSpec(
        name="write_script",
        description="Write a script file into the workspace",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = (self._workspace / input["path"]).resolve()
        if not path.is_relative_to(self._workspace.resolve()):
            return ToolResult(data=None, success=False, error="path escapes workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(input["content"], encoding="utf-8")
        return ToolResult(data={"path": str(path)})


class RunScriptTool(BaseTool):
    """在工作区沙箱执行脚本并返回输出。"""

    spec = ToolSpec(
        name="run_script",
        description="Run a script in the workspace sandbox and return output",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 120},
            },
            "required": ["path"],
        },
    )

    def __init__(self, workspace: Path, runtime=None) -> None:
        self._workspace = Path(workspace)
        self._runtime = runtime or LocalExperimentRuntime()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        output = await self._runtime.run(
            ExecutionRequest(
                entrypoint=input["path"],
                cwd=self._workspace,
                timeout_s=int(input.get("timeout_s", 120)),
            )
        )
        return ToolResult(
            data={
                "returncode": output.returncode,
                "stdout": output.stdout,
                "stderr": output.stderr,
                "files": output.files,
            }
        )


class CommitResultTool(BaseTool):
    """把一段结果文本提交为 artifact，返回其引用。"""

    spec = ToolSpec(
        name="commit_result",
        description="Commit result text to the artifact store and return its reference",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = await self._store.put_text(input["text"])
        return ToolResult(data={"ref": ref})

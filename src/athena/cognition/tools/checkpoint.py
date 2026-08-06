"""AgentCheckpointTool — mark a milestone checkpoint on a plan node."""

from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class AgentCheckpointTool(BaseTool):
    """Mark a milestone checkpoint when completing or failing a plan step."""

    def __init__(self, manager) -> None:
        self._manager = manager

    spec = ToolSpec(
        name="agent_checkpoint",
        description=(
            "Mark a milestone checkpoint on a plan step. Call after completing "
            "or failing a key step. Updates the plan tree status and records "
            "what was accomplished."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "The plan node ID to mark (from agent_plan output).",
                },
                "status": {
                    "type": "string",
                    "enum": ["completed", "failed"],
                    "description": "Outcome of this step.",
                },
                "summary": {
                    "type": "string",
                    "description": "What was accomplished, discovered, or why it failed.",
                },
                "artifact_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional references to key output files.",
                },
            },
            "required": ["node_id", "status", "summary"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> Any:
        """更新节点状态并记录检查点，返回进度统计。"""
        node_id = str(input.get("node_id", ""))
        status = str(input.get("status", "completed"))
        summary = str(input.get("summary", ""))
        artifact_refs = list(input.get("artifact_refs", []))

        node = await self._manager.update_node_status(node_id, status, evidence=summary)
        cp = await self._manager.create_checkpoint(node_id, summary, artifact_refs)
        track = await self._manager.get_current_position()

        return ToolResult(
            data={
                "node_id": node.id,
                "status": node.status.value,
                "checkpoint_id": cp.checkpoint_id,
                "progress": {
                    "completed": track.completed_count,
                    "failed": track.failed_count,
                    "pending": track.pending_count,
                },
                "next_node_id": track.current_node_id,
                "mermaid_file": str(self._manager._cog_dir / "plan.mermaid"),
            }
        )

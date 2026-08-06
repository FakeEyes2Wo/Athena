"""AgentTrackTool — query current execution progress."""

from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class AgentTrackTool(BaseTool):
    """Query current execution progress and next steps in the plan."""

    def __init__(self, manager) -> None:
        self._manager = manager

    spec = ToolSpec(
        name="agent_track",
        description=(
            "Query current execution progress. Use when you need to know "
            "where you are in the plan, what's next, or get a full summary."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "enum": ["position", "next", "summary"],
                    "description": (
                        "'position' for current location + path to root; "
                        "'next' for the next pending step(s); "
                        "'summary' for full progress statistics."
                    ),
                    "default": "summary",
                },
            },
            "additionalProperties": False,
        },
        concurrency_safe=True,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> Any:
        """按 query 返回位置 / 下一步 / 汇总三类进度信息。"""
        query = str(input.get("query", "summary"))

        if query == "position":
            ts = await self._manager.get_current_position()
            data = {
                "current_node_id": ts.current_node_id,
                "active_nodes": ts.active_nodes,
                "path_to_root": ts.path_to_root,
            }
        elif query == "next":
            next_nodes = await self._manager.get_next_pending()
            data = {
                "next_steps": [{"node_id": n.id, "title": n.title} for n in next_nodes],
            }
        else:  # summary
            ts = await self._manager.get_current_position()
            next_nodes = await self._manager.get_next_pending()
            data = {
                "completed": ts.completed_count,
                "failed": ts.failed_count,
                "active": len(ts.active_nodes),
                "pending": ts.pending_count,
                "current_node_id": ts.current_node_id,
                "next_steps": [
                    {"node_id": n.id, "title": n.title} for n in next_nodes[:5]
                ],
                "path_to_root": ts.path_to_root,
                "mermaid_file": str(self._manager._cog_dir / "plan.mermaid"),
            }
        return ToolResult(data=data)

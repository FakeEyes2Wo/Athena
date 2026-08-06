"""AgentPlanTool — create or update the execution plan tree."""

from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class AgentPlanTool(BaseTool):
    """Create a tree-structured execution plan before starting work."""

    def __init__(self, manager) -> None:
        self._manager = manager

    spec = ToolSpec(
        name="agent_plan",
        description=(
            "Create a tree-structured execution plan before starting work. "
            "Call this first to define goals and steps. Each step can have "
            "children for sub-steps, parallel tasks, or alternative approaches."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Top-level goal statement.",
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "Top-level execution steps. Each step has: "
                        "title (str), description (str, optional), "
                        "relation ('sequential'|'parallel'|'alternative'), "
                        "children (list of steps, optional recursive)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "relation": {
                                "type": "string",
                                "enum": ["sequential", "parallel", "alternative"],
                            },
                            "children": {"type": "array", "items": {}},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["goal", "steps"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> Any:
        """委托 Manager.create_plan 创建计划树，返回摘要与 mermaid 路径。"""
        goal = str(input.get("goal", ""))
        steps = list(input.get("steps", []))
        plan = await self._manager.create_plan(goal, steps)
        plan_summary = await self._manager.get_current_position()
        return ToolResult(
            data={
                "plan_id": plan.plan_id,
                "goal": plan.goal,
                "root_steps": [c.title for c in plan.root.children],
                "total_steps": plan_summary.completed_count
                + plan_summary.failed_count
                + plan_summary.pending_count
                + len(plan_summary.active_nodes),
                "mermaid_file": str(self._manager._cog_dir / "plan.mermaid"),
            }
        )

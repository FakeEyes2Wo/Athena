"""AgentReflectTool — review progress and adjust the plan."""

from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class AgentReflectTool(BaseTool):
    """Review progress, reflect on failures, and optionally adjust the plan."""

    def __init__(self, manager) -> None:
        self._manager = manager

    spec = ToolSpec(
        name="agent_reflect",
        description=(
            "Review progress and reflect on what's working or not. "
            "Use after repeated failures, major phase completions, or "
            "when you suspect the plan needs adjustment. Can add "
            "alternative approach branches to the plan tree."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "trigger": {
                    "type": "string",
                    "description": "Why this reflection was triggered.",
                },
                "observations": {
                    "type": "string",
                    "description": "What actually happened.",
                },
                "adjustment": {
                    "type": "string",
                    "description": "Proposed strategy change.",
                },
                "new_branches": {
                    "type": "array",
                    "description": (
                        "Optional new alternative branches to add. "
                        "Each has: parent_id (str, required), "
                        "title (str, required), description (str, optional)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "parent_id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["parent_id", "title"],
                    },
                },
            },
            "required": ["trigger", "observations"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> Any:
        """记录反思，并可选为计划树添加替代分支。"""
        trigger = str(input.get("trigger", ""))
        observations = str(input.get("observations", ""))
        adjustment = str(input.get("adjustment", ""))
        new_branches = list(input.get("new_branches", []))

        plan_changes = []
        for branch in new_branches:
            parent_id = str(branch["parent_id"])
            title = str(branch["title"])
            desc = str(branch.get("description", ""))
            node = await self._manager.add_alternative_branch(parent_id, title, desc)
            plan_changes.append(
                {
                    "action": "add_alternative",
                    "node_id": node.id,
                    "parent_id": parent_id,
                    "title": title,
                }
            )

        r = await self._manager.create_reflection(
            trigger, observations, adjustment, plan_changes
        )

        return ToolResult(
            data={
                "reflection_id": r.reflection_id,
                "trigger": r.trigger,
                "adjustment": r.adjustment,
                "plan_changes_made": len(plan_changes),
                "mermaid_file": str(self._manager._cog_dir / "plan.mermaid"),
            }
        )

"""AgentGuardTool — check actions against registered hard constraints."""

from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class AgentGuardTool(BaseTool):
    """Check if a planned action violates any registered hard constraints."""

    def __init__(self, manager) -> None:
        self._manager = manager

    spec = ToolSpec(
        name="agent_guard",
        description=(
            "Check if a planned action violates any registered hard "
            "constraints. Advisory only — does not block execution. "
            "Use before risky operations like deleting files, modifying "
            "evaluation scripts, or changing dataset splits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Description of the planned action.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional additional context.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> Any:
        """将动作描述交给 Manager.check_action 检查硬约束。"""
        action = str(input.get("action", ""))
        context = str(input.get("context", ""))
        full_desc = f"{action} {context}" if context else action
        result = await self._manager.check_action(full_desc)
        return ToolResult(
            data={
                "allowed": result.allowed,
                "violations": result.violations,
                "warnings": result.warnings,
            }
        )

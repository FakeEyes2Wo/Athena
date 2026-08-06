"""Agent cognition subsystem — register_cognition_tools() assembly entry.

Reference pattern: src/athena/tools/mcp/__init__.py register_mcp_tools().
"""

from athena.core.tool import ToolRegistry

from athena.cognition.manager import CognitionManager
from athena.cognition.tools.plan import AgentPlanTool
from athena.cognition.tools.checkpoint import AgentCheckpointTool
from athena.cognition.tools.track import AgentTrackTool
from athena.cognition.tools.reflect import AgentReflectTool
from athena.cognition.tools.guard import AgentGuardTool


async def register_cognition_tools(
    registry: ToolRegistry,
    state_store=None,
    work_root: str = "work",
) -> CognitionManager:
    """Create CognitionManager and register the five cognitive tools.

    Args:
        registry: ToolRegistry to register tools into.
        state_store: Optional StateStore (unused in v1; direct filesystem persistence).
        work_root: Working directory root for plan.json/plan.mermaid output.

    Returns:
        CognitionManager instance for optional hint integration or restore.
    """
    manager = CognitionManager(state_store=state_store, work_root=work_root)
    registry.register(AgentPlanTool(manager=manager))
    registry.register(AgentCheckpointTool(manager=manager))
    registry.register(AgentTrackTool(manager=manager))
    registry.register(AgentReflectTool(manager=manager))
    registry.register(AgentGuardTool(manager=manager))
    return manager

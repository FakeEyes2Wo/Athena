"""PREPARE EDA Agent registration.

PREPARE 阶段第一步：只做 EDA，写 ``EDA_HANDOFF.md`` 给 baseline_ideator。
"""

from collections.abc import Callable
from pathlib import Path

from athena.agents.ideator_agent import HandoffResult
from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime

PREPARE_EDA_AGENT_ID = "prepare_eda"
PREPARE_EDA_AGENT_TYPE = "prepare_eda"


def register_prepare_eda_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
) -> None:
    """Register a fresh PREPARE EDA Agent bound to the PREPARE worktree."""
    register_prompt_agent(
        registry,
        agent_type=PREPARE_EDA_AGENT_TYPE,
        output_type=HandoffResult,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


__all__ = [
    "PREPARE_EDA_AGENT_ID",
    "PREPARE_EDA_AGENT_TYPE",
    "register_prepare_eda_agent",
]

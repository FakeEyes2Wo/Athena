"""Autonomous dynamic-EDA Data Agent registration for the SEARCH phase.

SEARCH 的 Ideator 需要补充信息时（``HypothesisBatch.eda_request``），
``AgentTurnRunner`` 派发一个 Data Agent：工具绑定 PREPARE 产生的 EDA 工作区
目录，让它把补充分析写回同一目录（追加 report 小节 + 新 figures），后续
Ideator 回合再读取更新后的 EDA。
"""

from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.research_models import EdaResult
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime

DATA_AGENT_ID = "data"
DATA_AGENT_TYPE = "data"


def register_data_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh Data Agent factory bound to the EDA workspace."""
    register_prompt_agent(
        registry,
        agent_type=DATA_AGENT_TYPE,
        output_type=EdaResult,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


__all__ = [
    "DATA_AGENT_ID",
    "DATA_AGENT_TYPE",
    "register_data_agent",
]

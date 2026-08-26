"""Autonomous PREPARE Agent registration (prepare/evaluator/EDA workers)."""

from collections.abc import Callable
from pathlib import Path

from athena.agents.ideator_agent import HandoffResult
from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.plans import PlanDecision

PREPARE_AGENT_ID = "prepare"
PREPARE_AGENT_TYPE = "prepare"
EVALUATOR_AGENT_ID = "evaluator"
EVALUATOR_AGENT_TYPE = "evaluator"
PREPARE_EDA_AGENT_ID = "prepare_eda"
PREPARE_EDA_AGENT_TYPE = "prepare_eda"
EDA_WORKER_AGENT_TYPE = "eda_worker"


def register_prepare_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh PREPARE Agent factory with real workspace tools.

    ``extra_tools`` 追加领域工具（如 Kaggle），让 PREPARE 在做任务理解时能自行
    决定是否连接竞赛、下载数据并合成 SOTA 方案。
    """
    register_prompt_agent(
        registry,
        agent_type=PREPARE_AGENT_TYPE,
        output_type=PlanDecision,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


def register_evaluator_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh evaluator Agent factory writing the eval script draft.

    ``extra_tools`` 追加领域工具（如 Kaggle），让 evaluator 能查竞赛评估指标并
    取训练数据生成 ``labels.csv``。
    """
    register_prompt_agent(
        registry,
        agent_type=EVALUATOR_AGENT_TYPE,
        output_type=PlanDecision,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


def register_prepare_eda_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
) -> None:
    """Register the EDA orchestrator and its worker subagents."""
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
    register_prompt_agent(
        registry,
        agent_type=EDA_WORKER_AGENT_TYPE,
        output_type=HandoffResult,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
        name="eda-worker",
    )


__all__ = [
    "EDA_WORKER_AGENT_TYPE",
    "EVALUATOR_AGENT_ID",
    "EVALUATOR_AGENT_TYPE",
    "PREPARE_AGENT_ID",
    "PREPARE_AGENT_TYPE",
    "PREPARE_EDA_AGENT_ID",
    "PREPARE_EDA_AGENT_TYPE",
    "register_evaluator_agent",
    "register_prepare_agent",
    "register_prepare_eda_agent",
]

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
EDA_ORCHESTRATOR_MAX_TURNS = 12
EDA_WORKER_MAX_TURNS = 10
EDA_MAX_TOKENS = 2048


def _register(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    agent_type: str,
    output_type: type,
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
    name: str | None = None,
    max_turns: int = 200,
    max_tokens: int = 4096,
) -> None:
    """Single registration path for PREPARE-family prompt agents."""
    register_prompt_agent(
        registry,
        agent_type=agent_type,
        output_type=output_type,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
        name=name,
        max_turns=max_turns,
        max_tokens=max_tokens,
    )


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
    _register(
        registry,
        provider=provider,
        artifacts=artifacts,
        workspace=workspace,
        runtime=runtime,
        agent_type=PREPARE_AGENT_TYPE,
        output_type=PlanDecision,
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
    _register(
        registry,
        provider=provider,
        artifacts=artifacts,
        workspace=workspace,
        runtime=runtime,
        agent_type=EVALUATOR_AGENT_TYPE,
        output_type=PlanDecision,
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
    _register(
        registry,
        provider=provider,
        artifacts=artifacts,
        workspace=workspace,
        runtime=runtime,
        agent_type=PREPARE_EDA_AGENT_TYPE,
        output_type=HandoffResult,
        extra_tools=extra_tools,
        max_turns=EDA_ORCHESTRATOR_MAX_TURNS,
        max_tokens=EDA_MAX_TOKENS,
    )
    _register(
        registry,
        provider=provider,
        artifacts=artifacts,
        workspace=workspace,
        runtime=runtime,
        agent_type=EDA_WORKER_AGENT_TYPE,
        output_type=HandoffResult,
        extra_tools=extra_tools,
        name="eda-worker",
        max_turns=EDA_WORKER_MAX_TURNS,
        max_tokens=EDA_MAX_TOKENS,
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

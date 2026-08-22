"""DataClean PREPARE agent：分析给定数据并按实际情况清洗。"""

from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.plans import PlanDecision

DATACLEAN_AGENT_ID = "dataclean"
DATACLEAN_AGENT_TYPE = "dataclean"


def register_dataclean_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh DataClean Agent factory that writes a cleaning handoff.

    ``extra_tools`` 追加领域工具（Kaggle / HuggingFace / MCP），让 dataclean 能检索
    相似数据与预训练模型。完成 gate 由 ``run_dataclean_plan`` 按数据无关规则校验。
    """
    register_prompt_agent(
        registry,
        agent_type=DATACLEAN_AGENT_TYPE,
        output_type=PlanDecision,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


__all__ = [
    "DATACLEAN_AGENT_ID",
    "DATACLEAN_AGENT_TYPE",
    "register_dataclean_agent",
]

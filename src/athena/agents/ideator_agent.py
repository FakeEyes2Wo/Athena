"""Autonomous Ideator Agent registration.

SEARCH 空槽需要新假设时，ResearchRuntime 派一个 Ideator Agent：工具绑定 PREPARE
产生的 EDA 工作区目录，让它自行探索（读文件 / 跑命令），最后以 HypothesisBatch
结构化输出一组假设，经 ``Supervisor.register_hypotheses`` 写入图。
"""

from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.research_models import HypothesisBatch
from athena.execution.runtime import ExecutionRuntime

IDEATOR_AGENT_TYPE = "ideator"


def register_ideator_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
) -> None:
    """Register a fresh Ideator Agent factory bound to the EDA workspace."""
    register_prompt_agent(
        registry,
        agent_type=IDEATOR_AGENT_TYPE,
        output_type=HypothesisBatch,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
    )


__all__ = ["IDEATOR_AGENT_TYPE", "register_ideator_agent"]

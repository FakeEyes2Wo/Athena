"""Autonomous Ideator Agent registration.

SEARCH 空槽需要新假设时，ResearchRuntime 派一个 Ideator Agent：工具绑定 PREPARE
产生的 EDA 工作区目录，让它自行探索（读文件 / 跑命令），最后以 HypothesisBatch
结构化输出一组假设，经 ``Supervisor.register_hypotheses`` 写入图。
"""

from pathlib import Path

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.models import AgentConfig
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.contracts import ArtifactStore
from athena.core.research_models import HypothesisBatch
from athena.execution.runtime import ExecutionRuntime

IDEATOR_AGENT_TYPE = "ideator"
# 探索 + 生成一次假设的 ReAct 预算；放宽到与 code/plan 一致，足够深入探索 EDA 目录。
IDEATOR_MAX_TURNS = 200


def register_ideator_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
) -> None:
    """Register a fresh Ideator Agent factory bound to the EDA workspace.

    ``workspace`` 是 PREPARE 产出的 EDA 目录：工具（read_file / write_file /
    shell_command）都沙箱限定在该目录内，模型自行探索后输出 HypothesisBatch。
    """

    def factory(_agent_id: str, _config: str | None = None) -> AgentSpec:
        tools = generic_tool_registry(workspace, runtime=runtime)
        agent = Agent(
            provider,
            tools,
            load_prompt(IDEATOR_AGENT_TYPE),
            AgentConfig(name="ideator-agent", max_turns=IDEATOR_MAX_TURNS),
            output_type=HypothesisBatch,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(
                agent,
                tools=tools,
                agent_type=IDEATOR_AGENT_TYPE,
            ),
            codec=JsonCodec(),
        )

    registry.register(IDEATOR_AGENT_TYPE, factory)


__all__ = [
    "IDEATOR_AGENT_TYPE",
    "IDEATOR_MAX_TURNS",
    "register_ideator_agent",
]

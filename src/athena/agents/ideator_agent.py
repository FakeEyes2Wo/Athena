"""Autonomous Ideator Agent registration.

SEARCH 空槽需要新假设时，ResearchRuntime 派一个 Ideator Agent：工具绑定 PREPARE
产生的 EDA 工作区目录，让它自行探索（读文件 / 跑命令），最后以
``IdeatorHypothesisBatch``（见 ``research/idea_generation/idea_schemas.py``，带
premises/predictions/disconfirmers 的富结构）结构化输出一组假设。写入图之前先经
``research/idea_generation/gate.run_light_pipeline`` 过一遍 pre_gate + 视角审阅 +
light_hard_gate，再由 ``Supervisor.register_hypotheses`` 写入图。
"""

from collections.abc import Callable
from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.research_models import HypothesisBatch
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime
from athena.research.idea_generation.idea_schemas import IdeatorHypothesisBatch

IDEATOR_AGENT_TYPE = "ideator"
GATED_PROMPT_TYPE = "ideator_gated"


def register_ideator_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
    gated: bool = True,
) -> None:
    """Register a fresh Ideator Agent factory bound to the EDA workspace.

    ``gated``（默认）绑定 ``IdeatorHypothesisBatch`` 与 ``ideator_gated_agent.md``：
    要求 Ideator 交出 premises/predictions/disconfirmers，供下游门禁审计。
    ``gated=False`` 是消融对照组，回到 main 原有的 ``HypothesisBatch`` 与
    ``ideator_agent.md``——产出即入库，不过门禁。

    输出契约与 prompt 都在注册时绑定，所以开关必须在这一层，不能只在出口处分支。

    ``extra_tools`` 传零参 callable 时按 Agent 实例惰性求值：ideator 只注册一次，而
    文献语料要十几分钟才建好，冻结注册时刻的工具表等于让语料永远接不进来。
    """
    register_prompt_agent(
        registry,
        agent_type=IDEATOR_AGENT_TYPE,
        output_type=IdeatorHypothesisBatch if gated else HypothesisBatch,
        prompt_agent_type=GATED_PROMPT_TYPE if gated else IDEATOR_AGENT_TYPE,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


__all__ = ["GATED_PROMPT_TYPE", "IDEATOR_AGENT_TYPE", "register_ideator_agent"]

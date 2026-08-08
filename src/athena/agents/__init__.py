"""业务 Agent 实现（设计 registered-agent-catalog §3）。

首版静态注册以下类型，每个类型可有多个独立实例：

- ``supervisor``：动态编排项目内已注册 Agent。
- ``data``：数据检查、EDA、DataAnalysis 报告与版本提交。
- ``plot``：根据数据引用和图规格生成图片。
- ``reflection``：生成/追加 rubric，按指定版本评审。
- ``ideator``：生成、辩论、修订和演化假设。
- ``code``：在受限工作区生成或修订代码。
- ``report``：从已批准 Artifact 综合最终报告。

业务实现遵循 :class:`~athena.core.agent.runtime.BaseAgent`
``run(AgentContext) -> AgentOutcome`` 契约，由 :class:`~athena.agents.base_runner.BaseAgentRunner`
适配到 AgentKernel 的 runner。
"""

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.data_agent import DataAgent
from athena.agents.ideator import DebateResult, Ideator, IdeatorConfig
from athena.agents.orchestration import RunToolProjector
from athena.agents.reflection_agent import ReflectionAgent
from athena.agents.report_agent import ReportAgent
from athena.agents.simple_agents import CodeAgent, IdeatorAgent, PlotAgent
from athena.agents.supervisor import SupervisorAgent

__all__ = [
    "BaseAgentRunner",
    "CodeAgent",
    "DataAgent",
    "DebateResult",
    "Ideator",
    "IdeatorAgent",
    "IdeatorConfig",
    "PlotAgent",
    "ReflectionAgent",
    "ReportAgent",
    "RunToolProjector",
    "SupervisorAgent",
]

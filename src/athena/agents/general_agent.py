"""Autonomous General Agent registration.

通用杂活 worker，**不绑定任何固定 workspace**：工具根目录是项目根
（``project_root``），让它能看/改项目里的任意文件并跑命令，完成任意探索、
检查、生成、修复等杂活，最后以 ``GeneralResult`` 结构化汇报。Supervisor 需要
"让一个 worker 去看东西/干杂活"时经 ``dispatch_general`` 派发，而不是自己
持有读工具。
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.models import AgentConfig
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.contracts import ArtifactStore
from athena.execution.runtime import ExecutionRuntime

GENERAL_AGENT_TYPE = "general"
# 杂活可能要多轮探索/修复；与 ideator/plan 一致放宽到 200。
GENERAL_MAX_TURNS = 200


class GeneralResult(BaseModel):
    """General Agent 的通用杂活结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    result: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)


def register_general_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    project_root: Path,
    runtime: ExecutionRuntime,
) -> None:
    """Register a fresh General Agent factory rooted at ``project_root``.

    工具（``read_file`` / ``write_file`` / ``shell_command``）沙箱限定在
    ``project_root`` 内，不绑定任何具体 workspace；模型按派发请求自由探索、
    生成或修复，最后输出 ``GeneralResult``（``result`` 文本 + 可选的 ``files``）。
    """

    def factory(_agent_id: str, _config: str | None = None) -> AgentSpec:
        tools = generic_tool_registry(project_root, runtime=runtime)
        agent = Agent(
            provider,
            tools,
            load_prompt(GENERAL_AGENT_TYPE),
            AgentConfig(name="general-agent", max_turns=GENERAL_MAX_TURNS),
            output_type=GeneralResult,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(
                agent,
                tools=tools,
                agent_type=GENERAL_AGENT_TYPE,
            ),
            codec=JsonCodec(),
        )

    registry.register(GENERAL_AGENT_TYPE, factory)


__all__ = [
    "GENERAL_AGENT_TYPE",
    "GENERAL_MAX_TURNS",
    "GeneralResult",
    "register_general_agent",
]

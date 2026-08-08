"""按 agent_type 构造 LLM 驱动的 Agent（Codex-CLI 风格框架）。"""

from typing import TYPE_CHECKING

from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.runtime import Agent

if TYPE_CHECKING:
    from athena.core.agent.models import AgentConfig
    from athena.core.tool import ToolRegistry
    from athena.core.contracts import ArtifactStore


def create_agent_for(
    agent_type: str,
    *,
    model: str,
    tools: "ToolRegistry",
    system_prompt: str,
    output_type: type | None = None,
    artifacts: "ArtifactStore | None" = None,
    client=None,
    name: str | None = None,
) -> Agent:
    """构造一个 LLM 驱动的 Agent 实例。

    ``agent_type`` 用于命名与后续映射扩展；``output_type`` 非空时启用结构化输出。
    """
    provider = ResponsesProvider(model, client=client)
    return Agent(
        provider,
        tools,
        system_prompt,
        output_type=output_type,
        artifacts=artifacts,
    )

"""Autonomous Ideator Agent registration.

SEARCH 空槽需要新假设时，ResearchRuntime 按 ``IdeatorProfile`` 派发不同激进级别的
Ideator Agent：exploit / bold / moonshot。三个 profile 共用同一套 ``extra_tools``，
唯一区别是 ``prompt_agent_type`` 和输出契约；baseline_ideator 复用同一机制，只是
输出 ``HandoffResult``（写 handoff MD，不产 hypothesis）。

工具绑定 PREPARE 产生的 EDA 工作区目录；gated 模式下产出先经
``research/idea_generation/gate.run_light_pipeline`` 门禁，再写入 ResearchTree。
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.research_models import HypothesisBatch
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime
from athena.research.idea_generation.idea_schemas import IdeatorHypothesisBatch

IDEATOR_AGENT_TYPE = "ideator"
GATED_PROMPT_TYPE = "ideator_gated"


class HandoffResult(BaseModel):
    """Handoff Agent 的统一输出：写了哪个 MD 文件。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1)
    handoff_file: str


@dataclass(frozen=True)
class IdeatorProfile:
    """一个 ideator 变体：同一套工具，只换 prompt 与输出契约。"""

    agent_type: str
    prompt_agent_type: str
    output_type: type[BaseModel]
    task_hint: str


BASELINE_IDEATOR_PROFILE = IdeatorProfile(
    agent_type="baseline_ideator",
    prompt_agent_type="baseline_ideator",
    output_type=HandoffResult,
    task_hint=(
        "Design the strongest baseline architecture from the EDA handoff and "
        "your boldest prior knowledge. Do NOT propose hypotheses; write "
        "BASELINE_DESIGN.md."
    ),
)

EXPLOIT_IDEATOR_PROFILE = IdeatorProfile(
    agent_type="ideator_exploit",
    prompt_agent_type="ideator_exploit",
    output_type=IdeatorHypothesisBatch,
    task_hint=(
        "Improve the existing baseline: repairs, component changes, and "
        "concrete method changes."
    ),
)

BOLD_IDEATOR_PROFILE = IdeatorProfile(
    agent_type="ideator_bold",
    prompt_agent_type="ideator_bold",
    output_type=IdeatorHypothesisBatch,
    task_hint=(
        "Replace major components and methods. Include at least one complete "
        "architecture-replacement candidate per batch."
    ),
)

MOONSHOT_IDEATOR_PROFILE = IdeatorProfile(
    agent_type="ideator_moonshot",
    prompt_agent_type="ideator_moonshot",
    output_type=IdeatorHypothesisBatch,
    task_hint=(
        "Forget the baseline entirely. Propose completely new architectures or "
        "systems."
    ),
)

SEARCH_IDEATOR_PROFILES: tuple[IdeatorProfile, ...] = (
    EXPLOIT_IDEATOR_PROFILE,
    BOLD_IDEATOR_PROFILE,
    MOONSHOT_IDEATOR_PROFILE,
)


def register_ideator_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
    gated: bool = True,
    profile: IdeatorProfile | None = None,
) -> None:
    """Register a fresh Ideator Agent factory bound to the EDA workspace.

    ``profile=None`` 保持旧签名：``gated``（默认）绑定 ``IdeatorHypothesisBatch``
    与 ``ideator_gated_agent.md``；``gated=False`` 绑定 ``HypothesisBatch`` 与
    ``ideator_agent.md``。

    ``profile`` 提供时，agent_type / prompt / 输出契约全部来自 profile；
    ``extra_tools`` 对每个 profile 都一样（Kaggle + literature corpus）。

    输出契约与 prompt 都在注册时绑定，所以开关必须在这一层，不能只在出口处分支。

    ``extra_tools`` 传零参 callable 时按 Agent 实例惰性求值：ideator 只注册一次，而
    文献语料要十几分钟才建好，冻结注册时刻的工具表等于让语料永远接不进来。
    """
    agent_type = profile.agent_type if profile is not None else IDEATOR_AGENT_TYPE
    prompt_agent_type = (
        profile.prompt_agent_type
        if profile is not None
        else GATED_PROMPT_TYPE if gated else IDEATOR_AGENT_TYPE
    )
    output_type = (
        profile.output_type
        if profile is not None
        else IdeatorHypothesisBatch if gated else HypothesisBatch
    )
    register_prompt_agent(
        registry,
        agent_type=agent_type,
        output_type=output_type,
        prompt_agent_type=prompt_agent_type,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


__all__ = [
    "BASELINE_IDEATOR_PROFILE",
    "BOLD_IDEATOR_PROFILE",
    "EXPLOIT_IDEATOR_PROFILE",
    "GATED_PROMPT_TYPE",
    "HandoffResult",
    "IDEATOR_AGENT_TYPE",
    "IdeatorProfile",
    "MOONSHOT_IDEATOR_PROFILE",
    "SEARCH_IDEATOR_PROFILES",
    "register_ideator_agent",
]

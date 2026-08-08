"""生产 Ideator run_impl 构建器（设计方案2 §8：真实 Ideator 接入）。

真实 Ideator 需要模型凭据，且其输入（profile/ResearchTree）来自项目状态而非
AgentContext。本模块用 :class:`ProjectState` 协议桥接：每个 turn 从项目状态
解析输入，调用真实 ``Ideator.generate``，把 ``DebateResult`` 写为 Artifact 并
返回 ``AgentOutcome(result_ref)``。缺省仍使用确定性实现。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.storage.artifact_store import ArtifactStore


@dataclass(frozen=True)
class IdeatorInputs:
    """真实 Ideator.generate 的输入（来自项目 PREPARE/ResearchTree）。"""

    profile: Any
    papers: list[Any]
    models: list[Any]
    tree: Any


class ProjectState(Protocol):
    """生产 run_impl 所需的项目状态输入解析。"""

    async def ideator_inputs(self, ctx: AgentContext) -> IdeatorInputs: ...


Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


def ideator_run_impl(ideator: Any, store: ArtifactStore, project: ProjectState) -> Impl:
    """构建真实 Ideator 的 run_impl：委托 :class:`~athena.agents.ideator_agent.IdeatorAgent`。

    真实 Ideator 的接入逻辑（解析项目输入 → ``generate`` → 写 ``DebateResult``）
    收敛在 ``IdeatorAgent``；这里只构造接入了 ``ideator`` + ``project`` 的实例并
    返回其 ``run``，保持既有注入契约。
    """
    from athena.agents.ideator_agent import IdeatorAgent

    return IdeatorAgent(store, ideator=ideator, project=project).run

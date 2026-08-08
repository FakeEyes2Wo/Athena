"""生产 Ideator run_impl 构建器（设计方案2 §8：真实 Ideator 接入）。

真实 Ideator 需要模型凭据，且其输入（profile/ResearchTree）来自项目状态而非
AgentContext。本模块用 :class:`ProjectState` 协议桥接：每个 turn 从项目状态
解析输入，调用真实 ``Ideator.generate``，把 ``DebateResult`` 写为 Artifact 并
返回 ``AgentOutcome(result_ref)``。缺省仍使用确定性实现。
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.contracts import ArtifactStore


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
    """构建真实 Ideator 的 run_impl：解析项目输入 → generate → 写 DebateResult。"""

    async def _run(ctx: AgentContext) -> AgentOutcome:
        inputs = await project.ideator_inputs(ctx)
        result = await ideator.generate(
            inputs.profile,
            inputs.papers,
            inputs.models,
            inputs.tree,
        )
        result_ref = await store.put_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str)
        )
        return AgentOutcome(result_ref=result_ref)

    return _run

"""IdeatorAgent — 假设生成 Agent（设计 registered-agent-catalog §4.4）。

每个 ideator 实例生成可证伪 Hypothesis 并写入 Artifact，不覆盖历史假设；
多个辩者为多个独立实例，judge 为不同 name/config 的 ideator 实例。

真实 Ideator 已并入 agents 包（``athena.agents.ideator``）。本 Agent 经
``run_impl`` 接入真实 Ideator（由 :func:`~athena.agents.production.ideator_run_impl`
构建：从项目状态解析输入 → ``Ideator.generate`` → 写 ``DebateResult``）；
缺省保持确定性占位（把输入综合为 Hypothesis Artifact，PROPOSED）。
"""

import json
from collections.abc import Awaitable, Callable

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.storage.artifact_store import ArtifactStore

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


class IdeatorAgent(BaseAgent):
    """假设生成 Agent；可注入 run_impl，缺省确定性。"""

    def __init__(self, store: ArtifactStore, *, run_impl: Impl | None = None) -> None:
        self._store = store
        self._run_impl = run_impl

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        if self._run_impl is not None:
            return await self._run_impl(ctx)
        hypothesis = {"hypothesis": ctx.input_text or "", "status": "PROPOSED"}
        result_ref = await self._store.put_text(
            json.dumps(hypothesis, ensure_ascii=False)
        )
        return AgentOutcome(result_ref=result_ref)

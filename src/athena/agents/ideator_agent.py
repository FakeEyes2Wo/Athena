"""IdeatorAgent — 假设生成 Agent（设计 registered-agent-catalog §4.4）。

每个 ideator 实例生成可证伪 Hypothesis 并写入 Artifact，不覆盖历史假设；
多个辩者为多个独立实例，judge 为不同 name/config 的 ideator 实例。

真实 Ideator 已并入 agents 包（``athena.agents.ideator``）。本 Agent 接入真实
Ideator：注入 ``ideator``（真实 Ideator 实例）与 ``project``（实现
:class:`~athena.agents.production.ProjectState`，从项目状态解析输入）时，直接
运行辩论生成（proposal → review → revision → judge），把 ``DebateResult`` 写为
Artifact 返回 result_ref；否则可注入 ``run_impl``；缺省保持确定性占位（把输入
综合为 Hypothesis Artifact，PROPOSED）。
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.storage.artifact_store import ArtifactStore

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


class IdeatorAgent(BaseAgent):
    """假设生成 Agent；接入真实 Ideator，可注入 run_impl，缺省确定性。"""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        run_impl: Impl | None = None,
        ideator: Any = None,
        project: Any = None,
    ) -> None:
        self._store = store
        self._run_impl = run_impl
        self._ideator = ideator
        self._project = project

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        # 接入真实 Ideator：从项目状态解析输入 → generate → 写结果 Artifact
        if self._ideator is not None and self._project is not None:
            return await self._run_real(ctx)
        if self._run_impl is not None:
            return await self._run_impl(ctx)
        hypothesis = {"hypothesis": ctx.input_text or "", "status": "PROPOSED"}
        result_ref = await self._store.put_text(
            json.dumps(hypothesis, ensure_ascii=False)
        )
        return AgentOutcome(result_ref=result_ref)

    async def _run_real(self, ctx: AgentContext) -> AgentOutcome:
        """真实 Ideator 路径：project 提供输入，DebateResult 写为 Artifact。"""
        inputs = await self._project.ideator_inputs(ctx)
        result = await self._ideator.generate(
            inputs.profile,
            inputs.papers,
            inputs.models,
            inputs.tree,
        )
        result_ref = await self._store.put_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str)
        )
        return AgentOutcome(result_ref=result_ref)

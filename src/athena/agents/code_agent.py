"""CodeAgent — 受限工作区代码生成/修订 Agent（设计 registered-agent-catalog §4.5）。

同一 ``code`` 类型承担生成与基于失败证据的修订，修订 follow-up 原实例保留
工作区与尝试记忆。只产生候选 diff/运行 Artifact；Git 提交与接受由确定性
边界执行。

生产组合可注入 ``run_impl``（真实 CodeAgent，含 worktree/evaluator 与实验
输入解析）；缺省保持确定性实现（把输入写为候选 diff Artifact）。
"""

import json
from collections.abc import Awaitable, Callable

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.storage.artifact_store import ArtifactStore

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


class CodeAgent(BaseAgent):
    """代码生成/修订 Agent；可注入真实实现，缺省确定性。"""

    def __init__(self, store: ArtifactStore, *, run_impl: Impl | None = None) -> None:
        self._store = store
        self._run_impl = run_impl

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        if self._run_impl is not None:
            return await self._run_impl(ctx)
        diff = {"diff": ctx.input_text or "", "status": "candidate"}
        result_ref = await self._store.put_text(json.dumps(diff, ensure_ascii=False))
        return AgentOutcome(result_ref=result_ref)

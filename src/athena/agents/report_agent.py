"""ReportAgent — 最终报告综合 Agent（设计 registered-agent-catalog §4.6）。

只读取已批准 ArtifactRefs 综合叙述与引用，不运行实验、不修改指标、不补造
证据。正式版本为不可变目录 Bundle（``report.md``）；修订 follow-up 原
ReportAgent，新版本以 parent_ref 链接，旧版本不删除。

生产组合可注入 ``run_impl``（真实报告生成，从批准引用综合叙述）；
缺省保持确定性实现（把输入写为报告 Bundle）。
"""

from collections.abc import Awaitable, Callable

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.storage.artifact_store import ArtifactStore
from athena.storage.bundle import DirectoryBundle

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


class ReportAgent(BaseAgent):
    """最终报告 Agent；可注入真实实现，缺省确定性。"""

    def __init__(self, store: ArtifactStore, *, run_impl: Impl | None = None) -> None:
        self._store = store
        self._run_impl = run_impl
        self._latest_ref: str | None = None

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """把输入写为报告 Bundle 并返回其 ref；修订以 parent_ref 链接。"""
        if self._run_impl is not None:
            return await self._run_impl(ctx)
        text = ctx.input_text or ""
        result_ref = await DirectoryBundle.commit(
            self._store,
            {"report.md": await self._store.put_text(text)},
            parent_ref=self._latest_ref,
        )
        self._latest_ref = result_ref
        return AgentOutcome(result_ref=result_ref)

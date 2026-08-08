"""ReportAgent — 最终报告综合 Agent（设计 registered-agent-catalog §4.6）。

只读取已批准 ArtifactRefs 综合叙述与引用，不运行实验、不修改指标、不补造
证据。正式版本为不可变目录 Bundle（``report.md``）；修订 follow-up 原
ReportAgent，新版本以 parent_ref 链接，旧版本不删除。

真实路径（设计 prompt + UserInput 接入）：``model`` 为异步 ``(prompt) -> 报告
Markdown`` 可调用；``run`` 从触发消息的 ``content``/``context_refs``（UserInput）
收集已批准证据，用 :data:`DEFAULT_REPORT_PROMPT` 组装提示交给模型，把产出写为
报告 Bundle。缺省仍为确定性实现（把输入写为报告 Bundle）。
"""

from collections.abc import Awaitable, Callable

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.artifact_store import ArtifactNotFoundError, InvalidArtifactRefError
from athena.core.bundle import DirectoryBundle
from athena.core.contracts import ArtifactStore

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]
Model = Callable[[str], Awaitable[str]]

DEFAULT_REPORT_PROMPT = """\
你是 Athena 的最终报告撰写 Agent。根据下面提供的已批准证据引用及其内容，撰写一份
可追溯的 Markdown 科学报告。只使用提供的证据：不得虚构指标、实验、因果结论或引用。
请包含 Executive Summary、Evidence、Limitations、Recommendations 四个小节。不要输出
除报告正文以外的内容。

已批准证据：
{evidence}
"""


class ReportAgent(BaseAgent):
    """最终报告 Agent；可注入 run_impl 或 model，缺省确定性。"""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        run_impl: Impl | None = None,
        model: Model | None = None,
        prompt: str | None = None,
    ) -> None:
        self._store = store
        self._run_impl = run_impl
        self._model = model
        self._prompt = prompt or DEFAULT_REPORT_PROMPT
        self._latest_ref: str | None = None

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """把输入综合为报告 Bundle 并返回其 ref（注入 model 时走真实路径）。"""
        if self._run_impl is not None:
            return await self._run_impl(ctx)
        if self._model is not None:
            return await self._run_real(ctx)
        return await self._commit(ctx.input_text or "")

    async def _run_real(self, ctx: AgentContext) -> AgentOutcome:
        """真实路径：UserInput 收集证据 → 组装 prompt → 模型 → 写报告 Bundle。"""
        evidence = await self._load_evidence(ctx)
        report = await self._model(self._prompt.format(evidence=evidence))
        return await self._commit(report)

    async def _load_evidence(self, ctx: AgentContext) -> str:
        """从触发消息 content 与 context_refs 收集已批准证据文本。"""
        parts: list[str] = []
        if ctx.input_text:
            parts.append(ctx.input_text)
        for message in ctx.messages:
            for ref in message.context_refs:
                try:
                    parts.append(f"## {ref}\n{await self._store.get_text(ref)}")
                except (ArtifactNotFoundError, InvalidArtifactRefError):
                    # 引用 artifact 不存在/无效 → 标记不可读，不中断报告生成
                    parts.append(f"## {ref}\n(unreadable)")
        return "\n\n".join(parts) or ""

    async def _commit(self, report: str) -> AgentOutcome:
        result_ref = await DirectoryBundle.commit(
            self._store,
            {"report.md": await self._store.put_text(report)},
            parent_ref=self._latest_ref,
        )
        self._latest_ref = result_ref
        return AgentOutcome(result_ref=result_ref)

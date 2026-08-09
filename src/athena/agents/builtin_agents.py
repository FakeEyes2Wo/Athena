"""业务 Agent（Code / Ideator / Plot / Report）。

小型 Agent（Code/Ideator/Plot）遵循同一形态：``BaseAgent`` 子类，可选注入
``run_impl``（生产真实逻辑），缺省用确定性实现把输入写为 Artifact。代码生成/
修订（§4.5）与假设生成（§4.4）共享 JSON-sink 基类，仅缺省 payload 不同；通用
绘图（§3.2）直接写图片字节与 JSON payload。ReportAgent 是外层编排器：收集已
批准证据 → 驱动内层 LLM agent（prompt=``report_agent.md``）→ 提交 report Bundle。
其余复杂 Agent（data/init/reflection/supervisor）保持独立文件。
"""

import json
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from athena.agents.prompt_agent import build_llm_agent
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import Agent, BaseAgent
from athena.core.artifact_store import ArtifactNotFoundError, InvalidArtifactRefError
from athena.core.bundle import DirectoryBundle
from athena.core.contracts import ArtifactStore

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


class _JsonSinkAgent(BaseAgent):
    """可选注入真实 run_impl 的 JSON Artifact Agent 基类。

    子类只声明 ``_fallback(text) -> dict`` 缺省 payload;``run`` 优先委托
    ``run_impl``,否则把 fallback 结果写为 JSON Artifact 并返回其 ref。

    COMPAT: IdeatorAgent 确定性 fallback + run_impl 注入(首版无 LLM 的确定性骨架);
    清理条件: 真实 Ideator 全量接线后。
    """

    def __init__(self, store: ArtifactStore, *, run_impl: Impl | None = None) -> None:
        self._store = store
        self._run_impl = run_impl

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """生成候选结果并返回其 ArtifactRef；注入 run_impl 时委托真实实现。"""
        if self._run_impl is not None:
            return await self._run_impl(ctx)
        result_ref = await self._store.put_text(
            json.dumps(self._fallback(ctx.input_text or ""), ensure_ascii=False)
        )
        return AgentOutcome(result_ref=result_ref)

    def _fallback(self, text: str) -> dict:
        raise NotImplementedError


class CodeAgent(_JsonSinkAgent):
    """代码生成/修订 Agent（设计 registered-agent-catalog §4.5）。

    同一 ``code`` 类型承担生成与基于失败证据的修订，修订 follow-up 原实例
    保留工作区与尝试记忆。只产生候选 diff/运行 Artifact；Git 提交与接受由
    确定性边界执行。缺省确定性：把输入写为 candidate diff。
    """

    def _fallback(self, text: str) -> dict:
        return {"diff": text, "status": "candidate"}


class IdeatorAgent(_JsonSinkAgent):
    """假设生成 Agent（设计 registered-agent-catalog §4.4）。

    每个实例生成可证伪 Hypothesis 并写入 Artifact，不覆盖历史假设；真实
    Ideator 经 ``run_impl`` 接入（由 :func:`~athena.agents.production.ideator_run_impl`
    构建）。缺省确定性：把输入综合为 PROPOSED Hypothesis。
    """

    def _fallback(self, text: str) -> dict:
        return {"hypothesis": text, "status": "PROPOSED"}


class PlotAgent(BaseAgent):
    """通用绘图 Agent（设计 data-analysis-agent-workflow §3.2）。

    任何需要图片的 Agent 都可以按 ``agent_type="plot"`` 创建它。返回图片
    ArtifactRef、图注与观察；不拥有调用方的业务报告，也不能提交 DataAnalysis
    版本。首版为确定性实现（占位图字节），真实绘图由模型 + matplotlib 完成。
    """

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """按请求生成图片并返回其 ArtifactRef 与图注/观察。"""
        request = json.loads(ctx.input_text or "{}")
        figure = request.get("figure", "plot")
        image_ref = await self._store.put_bytes(str(figure).encode("utf-8"))
        payload = {
            "image_ref": image_ref,
            "caption": request.get("caption", ""),
            "observations": [f"figure: {figure}"],
        }
        result_ref = await self._store.put_text(json.dumps(payload, ensure_ascii=False))
        return AgentOutcome(result_ref=result_ref)


class ReportAgent(BaseAgent):
    """最终报告综合 Agent（prompt 驱动内层 LLM agent）。

    外层确定性编排（类似 DataAgent/InitAgent）：从触发消息收集已批准证据
    （``input_text`` + ``messages[].context_refs``）→ 构造内层 LLM ReAct agent
    （prompt=``report_agent.md`` + 通用工具，cwd=workspace）→ 运行（LLM 按 prompt
    写 ``report.md``）→ 收集 → 提交不可变目录 Bundle。证据同时落到
    ``workspace/evidence.md``，供内层 LLM 用 ``read_file`` 读取（prompt §inputs）。
    修订 follow-up 原实例，新版本以 ``parent_ref`` 链接，旧版本不删除。

    只综合已批准证据，不运行实验、不修改指标、不补造证据。
    """

    name = "report-agent"
    description = "综合已批准证据生成最终报告 Bundle"

    def __init__(
        self,
        store: ArtifactStore,
        *,
        model: str,
        client: Any = None,
        inner_builder: Callable[..., Agent] | None = None,
    ) -> None:
        self._store = store
        self._model = model
        self._client = client
        self._inner_builder = inner_builder or build_llm_agent
        self._latest_ref: str | None = None

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """驱动内层 LLM agent 产出报告，收集后提交 report Bundle。

        ``evidence.md`` 是证据的唯一传递路径（内层 LLM 用 ``read_file`` 读取，
        prompt §inputs 明确指示）；内层 input_text 只引用该文件，不内联证据。
        临时工作区在 finally 中清理（提交 Bundle 之后）。
        """
        evidence = await self._collect_evidence(ctx)
        workspace = Path(tempfile.mkdtemp(prefix="athena-report-"))
        try:
            # 证据落到工作区：内层 LLM 可 read_file 读取（prompt §inputs）
            (workspace / "evidence.md").write_text(evidence, encoding="utf-8")
            inner = self._inner_builder(
                "report", model=self._model, client=self._client, workspace=workspace
            )
            inner_ctx = AgentContext(
                thread=ctx.thread,
                turn=ctx.turn,
                emit=ctx.emit,
                tools=inner.tools,
                cancel=ctx.cancel,
                memory=ctx.memory,
                input_text="Write the final report. The evidence is in workspace/evidence.md — read it with read_file.",
            )
            await inner.run(inner_ctx)
            report_path = workspace / "report.md"
            if not report_path.is_file():
                raise RuntimeError("LLM did not produce report.md")
            report = report_path.read_text(encoding="utf-8")
            ref = await DirectoryBundle.commit(
                self._store,
                {"report.md": await self._store.put_text(report)},
                parent_ref=self._latest_ref,
            )
            self._latest_ref = ref
            return AgentOutcome(result_ref=ref)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def _collect_evidence(self, ctx: AgentContext) -> str:
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

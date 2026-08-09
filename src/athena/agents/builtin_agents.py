"""小型确定性业务 Agent（Code / Ideator / Plot）。

三个 agent 遵循同一形态：``BaseAgent`` 子类，可选注入 ``run_impl``（生产真实
逻辑），缺省用确定性实现把输入写为 Artifact。代码生成/修订（§4.5）与假设生成
（§4.4）共享 JSON-sink 基类，仅缺省 payload 不同；通用绘图（§3.2）直接写图片
字节与 JSON payload。复杂 Agent（data/init/reflection/report/supervisor）保持
独立文件。
"""

import json
from collections.abc import Awaitable, Callable

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
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

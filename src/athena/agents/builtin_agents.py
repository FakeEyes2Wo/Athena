"""业务 Agent（Code / Ideator / Plot）。

小型 Agent（Code/Ideator/Plot）遵循同一形态：``BaseAgent`` 子类，可选注入
``run_impl``（生产真实逻辑），缺省用确定性实现把输入写为 Artifact。代码生成/
修订（§4.5）与假设生成（§4.4）共享 JSON-sink 基类，仅缺省 payload 不同；通用
绘图（§3.2）直接写图片字节与 JSON payload。其余复杂 Agent（data/init/
reflection/supervisor）保持独立文件。
"""

import json
from collections.abc import Awaitable, Callable

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.contracts import ArtifactStore

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


class _JsonSinkAgent(BaseAgent):
    """委托真实 run_impl 的 JSON Artifact Agent 基类。

    ``run_impl`` 必填：生产不静默 fallback（real-search-worktree plan Task 3）。
    """

    def __init__(self, store: ArtifactStore, *, run_impl: Impl) -> None:
        self._store = store
        self._run_impl = run_impl

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """委托 run_impl 返回其 AgentOutcome。"""
        return await self._run_impl(ctx)


class CodeAgent(_JsonSinkAgent):
    """代码生成/修订 Agent（design registered-agent-catalog §4.5）。

    run_impl 由 composition root 注入（production.code_run_impl）；结果只含
    experiment_id/workspace/predictions_path/report_path，不接收 labels/score。
    """


class IdeatorAgent(_JsonSinkAgent):
    """假设生成 Agent（design registered-agent-catalog §4.4）。

    run_impl 由 composition root 注入（production.structured_ideator_run_impl），
    以 HypothesisBatch 结构化输出，不静默 fallback。
    """


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

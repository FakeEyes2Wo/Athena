"""通用绘图 Agent（设计 data-analysis-agent-workflow §3.2）。

任何需要图片的 Agent 都可以按 ``agent_type="plot"`` 创建它。返回图片 ArtifactRef、
图注与观察；不拥有调用方的业务报告，也不能提交 DataAnalysis 版本。首版为
确定性实现（占位图字节），真实绘图由模型 + matplotlib 完成。
"""

import json

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.storage.artifact_store import ArtifactStore


class PlotAgent(BaseAgent):
    """通用绘图 Agent（确定性实现）。"""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def run(self, ctx: AgentContext) -> AgentOutcome:
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

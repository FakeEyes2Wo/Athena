"""待实现：GEPA 离线提示优化层（arXiv:2507.19457，``pip install gepa``）。

优化对象为版本化 PromptBundle，永不进入 Session 关键路径。运行契约：
度量 mu 取冻结评测集（SPARBench 抽样 + AutoScholarQuery 抽样 + 自建领域
查询）上的文档级 F1；文本反馈为逐查询的"漏检 gold 论文清单 + 误检噪声
清单"；rollout 只重放通道响应缓存，不触发真实检索；候选 bundle 必须在
冻结集上严格超过现役 bundle 才允许晋升。GEPA 已验证弱到强迁移，rollout
可用小模型跑、部署时换强模型。
"""

from athena.core.schemas import ArtifactRef
from athena.research.academic_survey.schemas import PromptBundle


class SurveyPromptOptimizer:
    """GEPA 的离线包装器。"""

    async def optimize(
        self,
        bundle: PromptBundle,
        evalset_ref: ArtifactRef,
        budget: dict,
    ) -> PromptBundle:
        """按模块 docstring 所述契约运行 GEPA，返回候选 PromptBundle。"""
        raise NotImplementedError

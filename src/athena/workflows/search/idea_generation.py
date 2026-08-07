"""搜索论文和模型，为 SEARCH 循环生成可证伪假设。"""

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.data.types import DataProfile
from athena.ideator import Ideator
from athena.retrieval.store import Retriever
from athena.retrieval.types import HFModelRef, PaperRef


class PaperSearch:
    """搜索与任务相关的论文和模型。降级优雅。"""

    def __init__(self, retriever: Retriever | None = None) -> None:
        self._retriever = retriever or Retriever()

    async def search(self, query: str, n: int = 5) -> list[PaperRef]:
        """搜索论文；失败时由检索器依次回退数据源。"""
        return await self._retriever.search_papers(query, n=n)

    async def search_models(self, query: str, n: int = 5) -> list[HFModelRef]:
        """在 HuggingFace 中搜索相关的预训练模型。"""
        return await self._retriever.search_models(query, n=n)


async def generate_hypotheses(
    data_profile: DataProfile,
    papers: list[PaperRef],
    models: list[HFModelRef],
    tree: ResearchTree,
    *,
    ideator: Ideator,
) -> list[Hypothesis]:
    """生成 3-5 个结构化、可证伪假设并登记到 ResearchTree。"""
    result = await ideator.generate(data_profile, papers, models, tree)
    for hypothesis in result.hypotheses:
        tree.add_hypothesis(hypothesis)
    return result.hypotheses

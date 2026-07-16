"""检索通道的统一契约：arXiv / OpenAlex / Semantic Scholar / PubMed 等。

每个通道实现同一接口，供 ``AcademicSurveyService.retrieve`` 在老虎机预算分配下
并发扇出。通道响应必须按 (channel, query, date) 缓存：一是限流保护，二是让
GEPA rollout 只重放缓存、不触发真实网络检索。去重键为 DOI -> arXiv id ->
S2 corpus id 的降级链，由调用方统一执行。
"""

from abc import ABC, abstractmethod

from athena.research.academic_survey.schemas import PaperRecord


class ChannelAdapter(ABC):
    """单个文献检索源的最小接口。"""

    #: 通道名，进入 PaperRecord.meta 与响应缓存键。
    name: str

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[PaperRecord]:
        """按查询返回候选论文（judgment 留空壳，由 RelevanceJudge 填写）。"""

    @abstractmethod
    async def references(self, paper_id: str) -> list[PaperRecord]:
        """返回该论文引用的文献，供 RefChain 单跳/二跳扩展。"""

    @abstractmethod
    async def fetch_source(self, paper_id: str) -> bytes:
        """拉取原文文件（优先 LaTeX/HTML，否则 PDF），供 Markdownify 转换。"""

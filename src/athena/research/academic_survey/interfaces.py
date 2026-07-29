"""AcademicSurvey 的可替换检索通道与 LLM chain 契约。"""

import asyncio
from typing import Protocol

from athena.research.academic_survey.schemas import (
    CandidateObservation,
    JudgmentDraft,
    JudgmentBatchDraft,
    QueryEvolution,
    QueryPlan,
    RewrittenQuery,
    SearchPage,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
    SurveyRequest,
)


class ChannelAdapter(Protocol):
    """一个论文元数据检索通道。"""

    name: str
    version: str

    async def search(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        """执行一条检索查询并返回按原始名次排序的观察。"""

    async def search_page(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cursor: str | None,
        cancel: asyncio.Event,
    ) -> SearchPage:
        """执行一页检索；不支持分页的 adapter 返回空 next_cursor。"""

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        """返回一篇论文的单跳引用候选；不支持时返回空列表。"""


class SurveyChains(Protocol):
    """SPAR 回路中四个无状态 LangChain 模块。"""

    prompt_bundle_version: str
    model_revision: str

    async def understand(self, request: SurveyRequest) -> QueryPlan:
        """把研究主题解析成 criteria、queries 与排序意图。"""

    async def judge(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        candidate: SurveyCandidate,
    ) -> JudgmentDraft:
        """根据 title/abstract 返回逐项相关性草稿。"""

    async def judge_batch(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        candidates: list[SurveyCandidate],
    ) -> JudgmentBatchDraft:
        """批量返回逐候选独立草稿；候选 ID 必须与输入完全一致。"""

    async def rewrite(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        query: SearchQuery,
        channel: str,
    ) -> RewrittenQuery:
        """把语义查询改写成指定通道的安全查询文本。"""

    async def evolve(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        accepted: list[SurveyCandidate],
        searched_queries: list[str],
    ) -> QueryEvolution:
        """根据已确认论文生成新的检索视角。"""

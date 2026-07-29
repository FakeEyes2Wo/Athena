"""AcademicSurvey 的 LangChain 结构化 LLM 模块。"""

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from athena.research.academic_survey.cache import record_run_metric
from athena.research.academic_survey.schemas import (
    JudgmentDraft,
    JudgmentBatchDraft,
    PromptBundle,
    QueryEvolution,
    QueryPlan,
    RewrittenQuery,
    SearchQuery,
    SurveyCandidate,
    SurveyRequest,
)

DEFAULT_PROMPTS = PromptBundle(
    version="academic-survey-v6",
    query_understanding=(
        "You plan academic paper retrieval. Return independently verifiable "
        "relevance criteria that can each be decided from a paper title and "
        "abstract, plus a small set of focused queries. Return exactly one "
        "required criterion first: the paper must materially inform at least one "
        "major substantive aspect of the overall research request. Never make "
        "this core gate a conjunction of all requested methods, applications, "
        "comparisons, or desired evidence. Return no additional criteria; express "
        "narrower aspects through complementary retrieval queries. A paper "
        "may be relevant when it materially addresses one requested aspect; do "
        "not require every aspect unless the user explicitly requires their "
        "conjunction. "
        "Return one broad core query that preserves the request's main topic, "
        "then complementary queries for distinct methods, applications, history, "
        "comparisons, or recent work. Each non-core query must combine the central "
        "topic with only one such facet and omit the request's other facets. "
        "For survey or comprehensive-review intent, include one dedicated broad "
        "query family that combines only the central topic with survey, review, "
        "or overview terms and place that query family first. Keep the other "
        "facet queries. Do "
        "not turn metadata "
        "constraints or PaperSourcePolicy concerns such as source availability "
        "and visual artifacts into relevance criteria; those are enforced "
        "deterministically downstream. Use only the allowed channels: arxiv, "
        "openalex, semantic_scholar, pubmed. Treat explicit request constraints "
        "as hard constraints."
    ),
    channel_query_rewrite=(
        "Rewrite only the supplied retrieval facet for exactly one named academic "
        "search channel and return query text only. Optimize for high recall: keep "
        "one or two essential concept groups and use synonyms within a group, but "
        "do not add the other facets of the original research request. Adapters "
        "apply year, date, venue, and language constraints, so omit those filters. "
        "For arxiv, use a valid fielded search_query expression with no more than "
        "two AND operators; repeat the field prefix on every OR term and never "
        "use grouped field syntax such as all:(term1 OR term2). For openalex and "
        "semantic_scholar, use three to eight plain keywords without Boolean, "
        "field, or range syntax; for pubmed, use "
        "concise PubMed Boolean and field syntax. Do not emit "
        "endpoints, URLs, headers, API keys, or page-size controls."
    ),
    relevance_judgment=(
        "You are a strict academic relevance judge. Evaluate every supplied "
        "criterion using only the paper title and abstract. For met criteria, "
        "quote exact supporting text; for unmet criteria, quote exact contrary "
        "text. Use unknown when the supplied text cannot prove either outcome. "
        "Also score the paper's holistic relevance to the research request from "
        "0 to 1, following SPAR's brief relevance assessment: consider the "
        "request's intent, key concepts, and the paper's content; 0 means "
        "completely irrelevant and 1 means highly relevant. A score of 0.5 is "
        "the minimum for direct substantive relevance. Do not produce an "
        "overall verdict."
    ),
    query_evolution=(
        "Generate at most three focused follow-up queries from accepted papers: "
        "one for methods, one for applications, and one for limitations. Avoid "
        "queries that duplicate the supplied search history."
    ),
)


class LangChainSurveyChains:
    """用一个注入的 chat model 实现 SPAR 的四个结构化模块。"""

    def __init__(
        self,
        model: BaseChatModel,
        prompts: PromptBundle = DEFAULT_PROMPTS,
    ) -> None:
        self.prompt_bundle_version = prompts.version
        self.prompt_bundle = prompts
        self.model_revision = str(
            getattr(model, "model_name", None)
            or getattr(model, "model", None)
            or model._llm_type
        )
        self.token_usage = 0
        self._understand = ChatPromptTemplate.from_messages(
            [("system", prompts.query_understanding), ("human", "{request}")]
        ) | model.with_structured_output(QueryPlan, include_raw=True)
        self._judge = ChatPromptTemplate.from_messages(
            [
                ("system", prompts.relevance_judgment),
                ("human", "Request:\n{request}\nPlan:\n{plan}\nPaper:\n{paper}"),
            ]
        ) | model.with_structured_output(JudgmentDraft, include_raw=True)
        self._judge_batch = ChatPromptTemplate.from_messages(
            [
                ("system", prompts.relevance_judgment),
                (
                    "human",
                    "Request:\n{request}\nPlan:\n{plan}\nPapers:\n{papers}\n"
                    "Return exactly one independent judgment for every candidate_id.",
                ),
            ]
        ) | model.with_structured_output(JudgmentBatchDraft, include_raw=True)
        self._rewrite = ChatPromptTemplate.from_messages(
            [
                ("system", prompts.channel_query_rewrite),
                (
                    "human",
                    "Domain:\n{domain}\nPurpose:\n{purpose}\nChannel:\n{channel}\n"
                    "Retrieval facet:\n{query}",
                ),
            ]
        ) | model.with_structured_output(RewrittenQuery, include_raw=True)
        self._evolve = ChatPromptTemplate.from_messages(
            [
                ("system", prompts.query_evolution),
                (
                    "human",
                    "Request:\n{request}\nPlan:\n{plan}\nAccepted:\n"
                    "{accepted}\nSearched queries:\n{searched_queries}",
                ),
            ]
        ) | model.with_structured_output(QueryEvolution, include_raw=True)

    async def understand(self, request: SurveyRequest) -> QueryPlan:
        """调用 Query Understanding chain。"""
        return await self._invoke(
            self._understand, {"request": request.model_dump_json(indent=2)}
        )

    async def judge(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        candidate: SurveyCandidate,
    ) -> JudgmentDraft:
        """调用逐项相关性判断 chain。"""
        return await self._invoke(
            self._judge,
            {
                "request": json.dumps({"topic": request.topic}, ensure_ascii=False),
                "plan": json.dumps(
                    {
                        "criteria": [
                            criterion.model_dump(mode="json")
                            for criterion in plan.criteria
                        ]
                    },
                    ensure_ascii=False,
                ),
                "paper": json.dumps(
                    {"title": candidate.title, "abstract": candidate.abstract},
                    ensure_ascii=False,
                ),
            },
        )

    async def rewrite(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        query: SearchQuery,
        channel: str,
    ) -> RewrittenQuery:
        """调用安全的通道查询改写 chain。"""
        return await self._invoke(
            self._rewrite,
            {
                "domain": plan.domain,
                "purpose": query.purpose,
                "channel": channel,
                "query": query.text,
            },
        )

    async def judge_batch(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        candidates: list[SurveyCandidate],
    ) -> JudgmentBatchDraft:
        """批量判断候选，同时保持逐篇 ID、证据与分数。"""
        result = await self._invoke(
            self._judge_batch,
            {
                "request": json.dumps({"topic": request.topic}, ensure_ascii=False),
                "plan": json.dumps(
                    {
                        "criteria": [
                            criterion.model_dump(mode="json")
                            for criterion in plan.criteria
                        ]
                    },
                    ensure_ascii=False,
                ),
                "papers": json.dumps(
                    [
                        {
                            "candidate_id": candidate.candidate_id,
                            "title": candidate.title,
                            "abstract": candidate.abstract,
                        }
                        for candidate in candidates
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        expected = {candidate.candidate_id for candidate in candidates}
        actual = {item.candidate_id for item in result.judgments}
        if actual != expected or len(result.judgments) != len(candidates):
            raise ValueError("batch judgment candidate IDs do not match the input")
        return result

    async def evolve(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        accepted: list[SurveyCandidate],
        searched_queries: list[str],
    ) -> QueryEvolution:
        """调用 Query Evolution chain。"""
        return await self._invoke(
            self._evolve,
            {
                "request": request.model_dump_json(indent=2),
                "plan": plan.model_dump_json(indent=2),
                "accepted": "\n".join(
                    paper.model_dump_json(indent=2) for paper in accepted[:10]
                ),
                "searched_queries": "\n".join(searched_queries),
            },
        )

    async def _invoke(self, chain, values: dict):
        result = await chain.ainvoke(values)
        parsed = result.get("parsed")
        if parsed is None:
            error = result.get("parsing_error")
            raise ValueError(f"structured output failed: {error}")
        raw = result.get("raw")
        usage = getattr(raw, "usage_metadata", None) or {}
        tokens = int(
            usage.get("total_tokens")
            or usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        )
        self.token_usage += tokens
        record_run_metric("llm_tokens", tokens)
        return parsed

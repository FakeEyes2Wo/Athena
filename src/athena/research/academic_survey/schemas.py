"""AcademicSurvey 的请求、检索状态与持久化结果模型。"""

from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, Field, field_validator, model_validator

from athena.core.schemas import ArtifactRef
from athena.research.paper_source.schemas import (
    PaperIdentity,
    PaperSourcePolicy,
    SourceHint,
    normalize_arxiv_id,
    normalize_doi,
)

ChannelName: TypeAlias = Literal["arxiv", "openalex", "semantic_scholar", "pubmed"]
CriterionVerdict: TypeAlias = Literal["met", "unmet", "unknown"]
RelevanceVerdict: TypeAlias = Literal["relevant", "irrelevant", "uncertain"]


class SurveyConstraints(BaseModel):
    """检索阶段可确定执行的硬约束。"""

    year_from: int | None = Field(default=None, ge=1000, le=9999)
    year_to: int | None = Field(default=None, ge=1000, le=9999)
    venues: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)

    @field_validator("venues", "languages", "domains")
    @classmethod
    def clean_string_constraints(cls, values: list[str]) -> list[str]:
        """去掉空约束，并保持用户给出的首个顺序。"""
        cleaned = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def validate_year_range(self) -> Self:
        """拒绝反向年份区间。"""
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from must not exceed year_to")
        return self


class SurveyRequest(BaseModel):
    """Scheduler 持久化后交给 AcademicSurveyAgent 的输入。"""

    topic: str = Field(min_length=1)
    constraints: SurveyConstraints = Field(default_factory=SurveyConstraints)
    mode: Literal["fast", "diligent"] = "fast"
    task_metadata_ref: ArtifactRef | None = None
    objective_ref: ArtifactRef | None = None
    unresolved_constraints: list[str] = Field(default_factory=list)
    paper_source_policy: PaperSourcePolicy = Field(default_factory=PaperSourcePolicy)

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_blank(cls, value: str) -> str:
        """去掉主题两侧空白并拒绝空主题。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("topic must not be blank")
        return cleaned

    @field_validator("unresolved_constraints")
    @classmethod
    def clean_unresolved_constraints(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class RelevanceCriterion(BaseModel):
    """一个可独立核验的相关性判据。"""

    criterion_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    required: bool = True

    @field_validator("criterion_id", "description")
    @classmethod
    def criterion_text_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("criterion text must not be blank")
        return cleaned


class SearchQuery(BaseModel):
    """一次可分发给一个或多个检索通道的查询。"""

    query_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    purpose: Literal[
        "core", "method", "application", "comparison", "history", "recent"
    ] = "core"
    channels: list[ChannelName] = Field(min_length=1)

    @field_validator("query_id", "text")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        """规范化 query id 与文本两侧空白。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("query id and text must not be blank")
        return cleaned

    @field_validator("channels")
    @classmethod
    def channels_must_be_unique(cls, values: list[ChannelName]) -> list[ChannelName]:
        return list(dict.fromkeys(values))


class RankingIntent(BaseModel):
    """最终确定性排序的四项权重。"""

    rrf_weight: float = Field(default=0.55, ge=0)
    relevance_weight: float = Field(default=0.25, ge=0)
    authority_weight: float = Field(default=0.10, ge=0)
    recency_weight: float = Field(default=0.10, ge=0)

    @model_validator(mode="after")
    def normalize_weights(self) -> Self:
        """将非负权重归一化为总和 1。"""
        total = (
            self.rrf_weight
            + self.relevance_weight
            + self.authority_weight
            + self.recency_weight
        )
        if total <= 0:
            raise ValueError("at least one ranking weight must be positive")
        self.rrf_weight /= total
        self.relevance_weight /= total
        self.authority_weight /= total
        self.recency_weight /= total
        return self


class QueryPlan(BaseModel):
    """Query Understanding 产生的受校验检索计划。"""

    intent: Literal[
        "survey",
        "recent_advances",
        "early_works",
        "influential_works",
        "method_comparison",
        "targeted",
    ]
    domain: str = Field(min_length=1)
    temporal_description: str | None = None
    criteria: list[RelevanceCriterion] = Field(min_length=1)
    queries: list[SearchQuery] = Field(min_length=1)
    ranking_intent: RankingIntent = Field(default_factory=RankingIntent)

    @field_validator("domain")
    @classmethod
    def domain_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("domain must not be blank")
        return cleaned

    @model_validator(mode="after")
    def require_a_gate(self) -> Self:
        """每个计划至少包含一个 required criterion。"""
        if not any(criterion.required for criterion in self.criteria):
            raise ValueError("query plan requires at least one required criterion")
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        query_ids = [query.query_id for query in self.queries]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("criterion_id values must be unique")
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("query_id values must be unique")
        return self


class QueryEvolution(BaseModel):
    """一轮检索后产生的新视角查询。"""

    queries: list[SearchQuery] = Field(default_factory=list)


class RewrittenQuery(BaseModel):
    """某个检索通道可直接接受的安全查询文本。"""

    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("rewritten query must not be blank")
        lowered = cleaned.casefold()
        forbidden = ("://", "api_key=", "authorization:", "page_size=", "per-page=")
        if any(token in lowered for token in forbidden):
            raise ValueError("rewritten query contains transport controls")
        return cleaned


class ObservedIdentity(BaseModel):
    """通道看到的身份线索；允许尚无真实 ID 的标题-only 结果。"""

    arxiv_id: str | None = None
    doi: str | None = None
    s2_paper_id: str | None = None
    openalex_id: str | None = None
    pmid: str | None = None
    pmc_id: str | None = None
    title: str | None = None

    @field_validator("arxiv_id", mode="before")
    @classmethod
    def clean_arxiv_id(cls, value: object) -> object:
        """把通道给出的 arXiv 写法规范化为裸 ID。"""
        if not isinstance(value, str):
            return value
        return normalize_arxiv_id(value)[0] or None

    @field_validator("doi", mode="before")
    @classmethod
    def clean_doi(cls, value: object) -> object:
        """把通道给出的 DOI 规范化为裸小写写法。"""
        if not isinstance(value, str):
            return value
        return normalize_doi(value) or None

    @field_validator("s2_paper_id", "openalex_id", "pmid", "pmc_id", mode="before")
    @classmethod
    def clean_identifier(cls, value: object) -> object:
        """去掉其他标识符两侧空白并把空串置空。"""
        if not isinstance(value, str):
            return value
        return value.strip() or None

    def to_paper_identity(self) -> PaperIdentity | None:
        """有真实 ID 时构建 paper_source 身份，否则返回 None。"""
        if not any(
            (
                self.arxiv_id,
                self.doi,
                self.s2_paper_id,
                self.openalex_id,
                self.pmid,
                self.pmc_id,
            )
        ):
            return None
        return PaperIdentity(**self.model_dump())


class CandidateObservation(BaseModel):
    """一个通道在一条查询中返回的一篇论文。"""

    channel: ChannelName
    query_id: str = Field(min_length=1)
    query_text: str = Field(min_length=1)
    raw_rank: int = Field(ge=1)
    identity: ObservedIdentity
    title: str = Field(min_length=1)
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1000, le=9999)
    venue: str = ""
    language: str = ""
    citation_count: int | None = Field(default=None, ge=0)
    hints: list[SourceHint] = Field(default_factory=list)
    depth: Literal[0, 1] = 0
    raw_response_ref: ArtifactRef | None = None


class SurveyCandidate(BaseModel):
    """跨通道合并后、具有稳定真实身份的一篇候选论文。"""

    candidate_id: str
    identity: PaperIdentity
    title: str
    abstract: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str = ""
    language: str = ""
    citation_count: int | None = None
    observations: list[CandidateObservation]


class JudgmentEvidence(BaseModel):
    """相关性判断引用的 title 或 abstract 原文。"""

    field: Literal["title", "abstract"]
    quote: str = Field(min_length=1)


class CriterionJudgmentDraft(BaseModel):
    """LLM 对单个 criterion 的结构化判断草稿。"""

    criterion_id: str
    verdict: CriterionVerdict
    evidence: list[JudgmentEvidence] = Field(default_factory=list)


class JudgmentDraft(BaseModel):
    """LLM 返回的逐项判断；不允许直接决定总 verdict。"""

    criteria: list[CriterionJudgmentDraft]


class CriterionJudgment(BaseModel):
    """经过证据校验后的单项判断。"""

    criterion_id: str
    verdict: CriterionVerdict
    evidence: list[JudgmentEvidence] = Field(default_factory=list)


class RelevanceJudgment(BaseModel):
    """由确定性规则聚合的最终相关性判断。"""

    rubric_version: str
    prompt_bundle_version: str
    criteria: list[CriterionJudgment]
    verdict: RelevanceVerdict


class RankBreakdown(BaseModel):
    """一篇参考论文的可重放排序组成。"""

    formula_version: Literal["academic-survey-ranking-v1"] = (
        "academic-survey-ranking-v1"
    )
    raw_rrf: float
    citation_count: int | None = None
    publication_year: int | None = None
    rrf: float
    relevance: float
    authority: float
    recency: float
    final_score: float


class ReferencePaper(BaseModel):
    """SurveyCorpus 内联的最终参考论文索引。"""

    paper_id: str
    identity: PaperIdentity
    title: str
    abstract: str
    year: int | None = None
    venue: str = ""
    rank: int = Field(ge=1)
    rank_score: float
    rank_breakdown_ref: ArtifactRef
    judgment_ref: ArtifactRef
    retrieval_channels: list[ChannelName]
    matched_queries: list[str]


class SurveyStats(BaseModel):
    """一次 survey 的小型成本与结果摘要。"""

    rounds: int = Field(ge=0)
    observations: int = Field(ge=0)
    unique_candidates: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    constraint_rejected: int = Field(default=0, ge=0)
    constraint_unknown: int = Field(default=0, ge=0)
    possible_duplicates: int = Field(default=0, ge=0)
    relevant: int = Field(ge=0)
    irrelevant: int = Field(ge=0)
    uncertain: int = Field(ge=0)
    channel_pulls: dict[ChannelName, int] = Field(default_factory=dict)
    channel_rewards: dict[ChannelName, int] = Field(default_factory=dict)
    batch_allocations: list[dict[ChannelName, int]] = Field(default_factory=list)
    query_channel_batches: list[dict[str, str | int]] = Field(default_factory=list)
    query_diagnostics: list[str] = Field(default_factory=list)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    llm_tokens: int = Field(default=0, ge=0)
    refchain_seeds: int = Field(default=0, ge=0)
    refchain_observations: int = Field(default=0, ge=0)
    saturation_curve: list[int] = Field(default_factory=list)
    wall_seconds: float = Field(default=0, ge=0)
    stop_reason: str
    errors: list[str] = Field(default_factory=list)


class SurveyCorpus(BaseModel):
    """AcademicSurvey 交给下游和审计方的持久化语料索引。"""

    schema_version: Literal["1.0"] = "1.0"
    request_ref: ArtifactRef
    budget_ref: ArtifactRef
    query_plan_ref: ArtifactRef
    prompt_bundle_version: str
    prompt_bundle_ref: ArtifactRef
    budget_version: str
    status: Literal["complete", "partial"]
    reference_papers: list[ReferencePaper]
    candidate_ledger_ref: ArtifactRef
    judgment_ledger_ref: ArtifactRef
    stats_ref: ArtifactRef


class AcademicSurveyResult(BaseModel):
    """Agent Turn 的顶层结果。"""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["complete", "partial"]
    survey_corpus_ref: ArtifactRef
    paper_source_request_ref: ArtifactRef | None = None
    prompt_bundle_version: str
    stats_ref: ArtifactRef
    warnings: list[str] = Field(default_factory=list)


class PromptBundle(BaseModel):
    """在线 LangChain 模块使用的版本化提示集合。"""

    schema_version: Literal["1.0"] = "1.0"
    version: str = Field(min_length=1)
    query_understanding: str = Field(min_length=1)
    channel_query_rewrite: str = Field(min_length=1)
    relevance_judgment: str = Field(min_length=1)
    query_evolution: str = Field(min_length=1)
    parent_versions: list[str] = Field(default_factory=list)
    optimizer_run_ref: ArtifactRef | None = None

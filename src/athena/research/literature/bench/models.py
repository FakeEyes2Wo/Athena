"""Versioned inputs and reports for literature benchmarks."""

from typing import Literal

from pydantic import BaseModel, Field, computed_field

BENCH_SCHEMA_VERSION = "1.0"


class KnownItemQuery(BaseModel):
    """One paraphrased retrieval question and its accepted papers."""

    query_id: str
    question: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    gold: list[str] = Field(min_length=1)
    rationale: str = ""


class QuerySet(BaseModel):
    """A versioned collection of known-item questions."""

    name: str
    description: str = ""
    corpus_papers: int = Field(default=0, ge=0)
    queries: list[KnownItemQuery] = Field(min_length=1)


class QueryOutcome(BaseModel):
    """One query's paper-level ranking result."""

    query_id: str
    rank: int | None = None
    returned_papers: list[str] = Field(default_factory=list)
    error: str = ""


class ChannelScore(BaseModel):
    """A channel's raw outcomes with derived aggregate scores."""

    channel: str
    outcomes: list[QueryOutcome] = Field(default_factory=list)

    def _hits(self, cutoff: int) -> int:
        return sum(
            outcome.rank is not None and outcome.rank <= cutoff
            for outcome in self.outcomes
        )

    @computed_field
    @property
    def scored(self) -> int:
        return len(self.outcomes)

    @computed_field
    @property
    def hit_at_1(self) -> int:
        return self._hits(1)

    @computed_field
    @property
    def hit_at_3(self) -> int:
        return self._hits(3)

    @computed_field
    @property
    def hit_at_5(self) -> int:
        return self._hits(5)

    @computed_field
    @property
    def hit_at_10(self) -> int:
        return self._hits(10)

    @computed_field
    @property
    def missed(self) -> int:
        return sum(outcome.rank is None for outcome in self.outcomes)

    @computed_field
    @property
    def mrr(self) -> float:
        reciprocal = sum(
            1.0 / outcome.rank for outcome in self.outcomes if outcome.rank is not None
        )
        return reciprocal / self.scored if self.scored else 0.0


class RetrievalBenchReport(BaseModel):
    """Complete known-item benchmark result."""

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    query_set: str
    corpus_ref: str
    corpus_papers: int = Field(ge=0)
    corpus_chunks: int = Field(ge=0)
    embedding_model: str = ""
    unusable_queries: list[str] = Field(default_factory=list)
    channels: list[ChannelScore] = Field(default_factory=list)
    top_k: int = Field(default=10, ge=1)


class RecallQuerySet(BaseModel):
    """A survey topic and the provenance-declared papers it should recover."""

    name: str
    topic: str = Field(min_length=1)
    gold: list[str] = Field(min_length=1)
    gold_source: str = Field(min_length=1)


class StageRecall(BaseModel):
    """Recall and loss at one pipeline stage."""

    stage: Literal["in_pool", "judged_relevant", "delivered"]
    found: int = Field(ge=0)
    recall: float = Field(ge=0.0, le=1.0)
    lost_here: int = Field(ge=0)


class RecallReport(BaseModel):
    """Recall losses split across retrieval, judging, and delivery."""

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    query_set: str
    topic: str
    gold_source: str
    gold_total: int = Field(ge=0)
    pool_size: int = Field(ge=0)
    delivered_size: int = Field(ge=0)
    threshold: float = Field(ge=0.0, le=1.0)
    stages: list[StageRecall] = Field(default_factory=list)
    missing_from_pool: list[str] = Field(default_factory=list)
    scored_below_threshold: list[str] = Field(default_factory=list)
    dropped_at_delivery: list[str] = Field(default_factory=list)


class DeliveryOverlapReport(BaseModel):
    """Pairwise overlap across repeated delivery sets."""

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    query: str = ""
    label: str = ""
    runs: int = Field(ge=2)
    delivered: list[int]
    mean_jaccard: float = Field(ge=0.0, le=1.0)
    min_jaccard: float = Field(ge=0.0, le=1.0)
    stable_core: list[str] = Field(default_factory=list)
    union_size: int = Field(ge=0)


class PaperHealth(BaseModel):
    """Structural health of one paper in a corpus."""

    paper_id: str
    title: str = ""
    chunks: int = Field(ge=0)
    sentences: int = Field(ge=0)
    has_abstract_chunk: bool
    anchor_kind: str = ""
    anchor_prose_chars: int = Field(ge=0)
    sections: list[str] = Field(default_factory=list)
    outbound_citations: int = Field(ge=0)
    visual_links: int = Field(ge=0)


class CorpusHealthReport(BaseModel):
    """Structural health of a loaded paper corpus."""

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    corpus_ref: str
    papers: int = Field(ge=0)
    chunks: int = Field(ge=0)
    sentences: int = Field(ge=0)
    semantic_search: bool
    embedding_model: str = ""
    abstract_coverage: float = Field(ge=0.0, le=1.0)
    thin_anchor_papers: int = Field(ge=0)
    citation_edges: int = Field(ge=0)
    citation_density: float = Field(ge=0.0)
    visual_links: int = Field(ge=0)
    section_coverage: dict[str, int] = Field(default_factory=dict)
    papers_detail: list[PaperHealth] = Field(default_factory=list)

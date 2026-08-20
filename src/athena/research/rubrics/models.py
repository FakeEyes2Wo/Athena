"""Strict structured contracts for both Rubric V2 layers."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from athena.core.contracts import ArtifactRef, NonBlankText

MetricDirection = Literal["maximize", "minimize"]
MetricSource = Literal["human", "official", "protocol", "ai"]


class ResearchEvaluationContext(BaseModel):
    """Trusted context assembled before PREPARE and evaluator freeze."""

    model_config = ConfigDict(extra="forbid", strict=True)

    research_task: NonBlankText
    task_understanding: dict[str, object] = Field(default_factory=dict)
    human_primary_metric: str | None = None
    human_direction: MetricDirection | None = None
    official_primary_metric: str | None = None
    official_direction: MetricDirection | None = None
    protocol_primary_metric: str | None = None
    protocol_direction: MetricDirection | None = None
    dataset_context: dict[str, object] = Field(default_factory=dict)
    evaluation_feasibility: list[str] = Field(default_factory=list)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    supported_metrics: dict[str, MetricDirection] = Field(default_factory=dict)


class EvaluationRubricDraft(BaseModel):
    """LLM scientific recommendation before deterministic precedence is applied."""

    model_config = ConfigDict(extra="forbid", strict=True)

    primary_metric: NonBlankText
    direction: MetricDirection
    secondary_metrics: list[NonBlankText] = Field(default_factory=list)
    guardrails: list[NonBlankText] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    explanation: NonBlankText
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)

    @field_validator("weights")
    @classmethod
    def _validate_weights(cls, value: dict[str, float]) -> dict[str, float]:
        for name, weight in value.items():
            if not name.strip():
                raise ValueError("weight names must be nonblank")
            if not 0.0 <= weight <= 1.0:
                raise ValueError("weight recommendations must be in [0,1]")
        return value


class EvaluationPolicy(BaseModel):
    """Frozen single-primary evaluation policy consumed by Athena runtime."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    primary_metric: NonBlankText
    direction: MetricDirection
    metric_source: MetricSource
    locked: bool
    secondary_metrics: list[NonBlankText] = Field(default_factory=list)
    guardrails: list[NonBlankText] = Field(default_factory=list)
    selection_mode: Literal["primary"] = "primary"
    weights: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    explanation: NonBlankText
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_lock_source(self) -> "EvaluationPolicy":
        should_lock = self.metric_source != "ai"
        if self.locked != should_lock:
            raise ValueError("human/official/protocol policies must be locked")
        return self


class HypothesisRubric(BaseModel):
    """One LLM review; its dimensions are deterministically aggregated later."""

    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_id: NonBlankText
    verifiability: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    historical_difference: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    eda_evidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    feasibility: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    cost_penalty: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    leakage_risk: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    explanation: NonBlankText
    dimension_reasons: dict[str, NonBlankText] = Field(default_factory=dict)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)


class HypothesisRankingBatch(BaseModel):
    """Batch response whose IDs must exactly cover the requested candidates."""

    model_config = ConfigDict(extra="forbid", strict=True)

    reviews: list[HypothesisRubric]

    @model_validator(mode="after")
    def _reject_duplicate_ids(self) -> "HypothesisRankingBatch":
        identifiers = [review.hypothesis_id for review in self.reviews]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("hypothesis rubric response contains duplicate IDs")
        return self


class HypothesisRankingCandidate(BaseModel):
    """Bounded candidate view supplied to the ranking LLM."""

    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_id: NonBlankText
    statement: NonBlankText
    intervention: NonBlankText
    expected_effect: NonBlankText
    cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    sources: list[str] = Field(default_factory=list)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)


class HypothesisRankingContext(BaseModel):
    """Research evidence visible to a post-Gate batch ranking review."""

    model_config = ConfigDict(extra="forbid", strict=True)

    research_task: NonBlankText
    evaluation_policy: EvaluationPolicy
    hypotheses: list[HypothesisRankingCandidate]
    eda_context: str = ""
    evaluator_handoff: str = ""
    baseline: dict[str, object] = Field(default_factory=dict)
    current_sota: dict[str, object] = Field(default_factory=dict)
    research_history: list[dict[str, object]] = Field(default_factory=list)
    environment_context: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "EvaluationPolicy",
    "EvaluationRubricDraft",
    "HypothesisRankingBatch",
    "HypothesisRankingCandidate",
    "HypothesisRankingContext",
    "HypothesisRubric",
    "MetricDirection",
    "MetricSource",
    "ResearchEvaluationContext",
]

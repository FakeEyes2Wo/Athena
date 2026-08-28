"""Strict contracts for task readiness and the two Rubric V2 layers."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.core.contracts import ArtifactRef, NonBlankText
from athena.core.research_models import MetricDirection, TaskUnderstanding

MetricPolicySource = Literal["human", "official", "protocol", "ai"]


class ResearchEvaluationContext(BaseModel):
    """Trusted context assembled before evaluator freeze."""

    model_config = ConfigDict(extra="forbid", strict=True)

    research_task: NonBlankText
    task_understanding: TaskUnderstanding
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
    """One AI recommendation before deterministic precedence is applied."""

    model_config = ConfigDict(extra="forbid", strict=True)

    primary_metric: NonBlankText
    direction: MetricDirection
    secondary_metrics: list[NonBlankText] = Field(default_factory=list)
    guardrails: list[NonBlankText] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    explanation: NonBlankText
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)


class EvaluationPolicy(BaseModel):
    """Frozen single-primary evaluation policy consumed by every later phase."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    primary_metric: NonBlankText
    direction: MetricDirection
    metric_source: MetricPolicySource
    locked: bool
    secondary_metrics: list[NonBlankText] = Field(default_factory=list)
    guardrails: list[NonBlankText] = Field(default_factory=list)
    selection_mode: Literal["primary"] = "primary"
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    explanation: NonBlankText
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_lock_source(self) -> "EvaluationPolicy":
        if self.locked != (self.metric_source != "ai"):
            raise ValueError("human/official/protocol policies must be locked")
        return self


class ResourceEstimate(BaseModel):
    """Normalized execution penalties retained as an auditable breakdown."""

    model_config = ConfigDict(extra="forbid", strict=True)

    latency_penalty: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    compute_penalty: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    memory_penalty: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    api_cost_penalty: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    implementation_penalty: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    explanation: NonBlankText


class HypothesisPriorityReview(BaseModel):
    """One four-dimension review; aggregation remains deterministic."""

    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_id: NonBlankText
    evidence_testability: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    scientific_value: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    resources: ResourceEstimate
    validity_risk_control: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    explanation: NonBlankText
    dimension_reasons: dict[str, NonBlankText] = Field(default_factory=dict)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)


class HypothesisPriorityBatch(BaseModel):
    """One batch response that must exactly cover the requested hypotheses."""

    model_config = ConfigDict(extra="forbid", strict=True)

    reviews: list[HypothesisPriorityReview] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_duplicate_ids(self) -> "HypothesisPriorityBatch":
        identifiers = [review.hypothesis_id for review in self.reviews]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("hypothesis priority response contains duplicate IDs")
        return self


class HypothesisPriorityCandidate(BaseModel):
    """Bounded candidate view supplied to the batch review."""

    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_id: NonBlankText
    statement: NonBlankText
    intervention: NonBlankText
    expected_effect: NonBlankText
    cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    sources: list[str] = Field(default_factory=list)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    gate_context: dict[str, object] = Field(default_factory=dict)


class HypothesisPriorityContext(BaseModel):
    """Bounded evidence visible to one post-Gate batch priority review."""

    model_config = ConfigDict(extra="forbid", strict=True)

    research_task: NonBlankText
    evaluation_policy: EvaluationPolicy
    hypotheses: list[HypothesisPriorityCandidate]
    eda_context: str = ""
    evaluator_handoff: str = ""
    baseline: dict[str, object] = Field(default_factory=dict)
    current_sota: dict[str, object] = Field(default_factory=dict)
    research_history: list[dict[str, object]] = Field(default_factory=list)
    environment_context: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "EvaluationPolicy",
    "EvaluationRubricDraft",
    "HypothesisPriorityBatch",
    "HypothesisPriorityCandidate",
    "HypothesisPriorityContext",
    "HypothesisPriorityReview",
    "MetricPolicySource",
    "ResearchEvaluationContext",
    "ResourceEstimate",
]

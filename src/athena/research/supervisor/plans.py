"""Durable Pydantic contracts shared by Plan execution and settlement."""

from dataclasses import dataclass
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from athena.core.artifact_store import digest_from_ref
from athena.core.contracts import ArtifactRef, CommitHash
from athena.core.research_models import Hypothesis
from athena.research.supervisor.prompt_context import failure_block

DEFAULT_EXPERIMENT_TIMEOUT_S = 3600
"""Default wall-clock budget for one experiment command (seconds)."""


@dataclass(frozen=True)
class PlanFailure:
    """Value object pairing a Plan failure kind with diagnostic detail."""

    kind: str
    detail: str

    @classmethod
    def from_summary(cls, summary: str | None) -> "PlanFailure | None":
        """Parse the durable compact failure summary, if present."""
        if not summary:
            return None
        kind, separator, detail = summary.partition(": ")
        if not separator:
            return cls(kind="failed", detail=summary)
        return cls(kind=kind or "failed", detail=detail or summary)

    def to_summary(self) -> str:
        """Render the compact value stored on durable Plan state."""
        return f"{self.kind}: {self.detail}"

    def to_prompt_block(self) -> str:
        """Render the prior failure as model-visible repair context."""
        return failure_block(self.kind, self.detail)


class _FrozenHypothesis(Hypothesis):
    """Plan-owned immutable copy of an existing Hypothesis contract."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    evidence_refs: tuple[ArtifactRef, ...] = ()
    supersedes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @field_validator("evidence_refs", "supersedes", "sources", mode="before")
    @classmethod
    def _freeze_collection(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class PlanState(BaseModel):
    """Persisted state for one unsettled Plan."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["PREPARE", "SEARCH", "VALIDATE"]
    context_ref: ArtifactRef
    turns_used: int = Field(ge=0)
    turn_limit: int | None = Field(ge=0)
    patience: int | None = Field(default=None, ge=0)
    stale_rounds: int = Field(default=0, ge=0)
    best_ref: ArtifactRef | None = None
    last_failure: str | None = Field(default=None, max_length=1200)

    @field_validator("context_ref", "best_ref")
    @classmethod
    def _validate_artifact_ref(cls, value: ArtifactRef | None) -> ArtifactRef | None:
        if value is not None:
            digest_from_ref(value)
        return value

    @model_validator(mode="after")
    def _validate_search_only_fields(self) -> "PlanState":
        search_fields = {"patience", "stale_rounds", "best_ref"}
        if self.kind == "SEARCH":
            if self.patience is None:
                raise ValueError("SEARCH Plan requires patience")
        elif search_fields & self.model_fields_set:
            raise ValueError("patience, stale_rounds and best_ref are SEARCH-only")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        payload = handler(self)
        if self.kind != "SEARCH":
            for field in ("patience", "stale_rounds", "best_ref"):
                payload.pop(field, None)
        if self.last_failure is None:
            payload.pop("last_failure", None)
        return payload


class PlanInput(BaseModel):
    """Frozen input artifact created with a Plan."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    hypothesis: _FrozenHypothesis | None = None
    active_ancestor_hypotheses: tuple[_FrozenHypothesis, ...] = ()
    reference_experiment_id: str | None = None
    reference_metric: float | None = None
    reference_priority: float = 0.0
    direction: Literal["maximize", "minimize"] = "maximize"
    tolerance: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    min_effect_size: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    alpha: float = Field(default=0.05, gt=0, le=1)
    family_size: int = Field(default=1, ge=1)
    evaluator_ref: ArtifactRef
    tree_ref: ArtifactRef
    eval_handoff: str = ""
    human_context: str = ""
    task_context: str = ""
    initial_turn_limit: int | None = Field(default=None, ge=0)
    initial_patience: int | None = Field(default=None, ge=0)

    @field_validator("hypothesis", mode="before")
    @classmethod
    def _freeze_hypothesis(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, Hypothesis):
            return value
        payload = value.model_dump(mode="python")
        for field in ("evidence_refs", "supersedes", "sources"):
            payload[field] = tuple(payload[field])
        return payload

    @field_validator("active_ancestor_hypotheses", mode="before")
    @classmethod
    def _freeze_ancestors(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(cls._freeze_hypothesis(hypothesis) for hypothesis in value)

    @field_validator("evaluator_ref", "tree_ref")
    @classmethod
    def _validate_artifact_ref(cls, value: ArtifactRef) -> ArtifactRef:
        digest_from_ref(value)
        return value


class PlanBest(BaseModel):
    """Immutable record of a Plan's best trusted score."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    metric: float = Field(allow_inf_nan=False)
    commit: CommitHash
    evidence_ref: ArtifactRef
    std_error: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    n: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_evidence_ref(self) -> "PlanBest":
        digest_from_ref(self.evidence_ref)
        return self


class PlanDecision(BaseModel):
    """Structured decision returned after every PlanAgent turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["continue", "submit", "abandon"]
    reason: str
    suggestions: list[str] = Field(default_factory=list)

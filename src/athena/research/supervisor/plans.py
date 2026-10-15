"""Durable Pydantic contracts shared by Plan execution and settlement."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
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

SEARCH_TURN_LIMIT = 12
"""Turn ceiling for one SEARCH Plan when its Hypothesis sets none.

SEARCH Plans previously ran without any turn ceiling and relied on ``patience``
alone.  With the default ``patience=0`` that gate degenerates: it settles the
Plan on the first turn that produced no trusted score, so a candidate that is
still implementing is settled as a failure, while the guard-rejection retry
path stays unbounded.  The ceiling bounds the retry paths so a Plan cannot loop
forever.
"""

REJECTION_RETRY_LIMIT = 8
"""Consecutive candidate-quality rejections a SEARCH Plan may repair.

A guard rejection (``no_change`` or ``diff_rejected``) means the turn produced
no measurable experiment, which is a reason to keep working rather than a
result.  The best candidate of a 40-slot run needed five consecutive
rejections to wire its module into the frozen command, so the allowance has to
sit above that; it is finite so a Plan that never implements anything still
settles and releases its slot.
"""

_LEGACY_PLAN_INPUT_FIELDS = frozenset(
    {"active_ancestor_hypotheses", "initial_turn_limit", "initial_patience"}
)


def _trusted_ref(value: ArtifactRef) -> ArtifactRef:
    digest_from_ref(value)
    return value


_TrustedArtifactRef = Annotated[ArtifactRef, AfterValidator(_trusted_ref)]


@dataclass(frozen=True, slots=True)
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
    context_ref: _TrustedArtifactRef
    turns_used: int = Field(ge=0)
    turn_limit: int | None = Field(ge=0)
    patience: int | None = Field(default=None, ge=0)
    stale_rounds: int = Field(default=0, ge=0)
    rejected_rounds: int = Field(default=0, ge=0)
    best_ref: _TrustedArtifactRef | None = None
    last_failure: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def _validate_search_only_fields(self) -> "PlanState":
        search_fields = {"patience", "stale_rounds", "rejected_rounds", "best_ref"}
        if self.kind == "SEARCH":
            if self.patience is None:
                raise ValueError("SEARCH Plan requires patience")
        elif search_fields & self.model_fields_set:
            raise ValueError(
                "patience, stale_rounds, rejected_rounds and best_ref are SEARCH-only"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        payload = handler(self)
        if self.kind != "SEARCH":
            for field in ("patience", "stale_rounds", "rejected_rounds", "best_ref"):
                payload.pop(field, None)
        if self.last_failure is None:
            payload.pop("last_failure", None)
        return payload


class PlanInput(BaseModel):
    """Frozen input artifact created with a Plan."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    hypothesis: _FrozenHypothesis | None = None
    reference_experiment_id: str | None = None
    reference_metric: float | None = None
    reference_priority: float = 0.0
    direction: Literal["maximize", "minimize"] = "maximize"
    tolerance: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    min_effect_size: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    alpha: float = Field(default=0.05, gt=0, le=1)
    family_size: int = Field(default=1, ge=1)
    evaluator_ref: _TrustedArtifactRef
    tree_ref: _TrustedArtifactRef
    eval_handoff: str = ""
    human_context: str = ""
    task_context: str = ""

    @model_validator(mode="before")
    @classmethod
    def _discard_legacy_fields(cls, value: object) -> object:
        if not isinstance(value, dict) or not (
            _LEGACY_PLAN_INPUT_FIELDS & value.keys()
        ):
            return value
        return {
            key: item
            for key, item in value.items()
            if key not in _LEGACY_PLAN_INPUT_FIELDS
        }

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


class PlanBest(BaseModel):
    """Immutable record of a Plan's best trusted score."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    metric: float = Field(allow_inf_nan=False)
    commit: CommitHash
    evidence_ref: _TrustedArtifactRef
    std_error: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    n: int | None = Field(default=None, ge=0)


class PlanDecision(BaseModel):
    """Structured decision returned after every PlanAgent turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["continue", "submit", "abandon"]
    reason: str


def plan_budget_note(*, turn: int, max_turns: int) -> str:
    """Escalating budget warning appended to a non-submit repair feedback.

    A PlanAgent turn that answers ``continue`` after its artifact is already in
    place costs the whole turn and produces no new information for the model;
    without a visible countdown a thorough Agent can spend the entire budget
    re-verifying and fail the Plan with the work actually complete.  The note
    is delivered at the start of the *next* turn, so it reports the turns the
    Agent still has left to act on.
    """
    remaining = max(0, max_turns - turn - 1)
    if remaining <= 2:
        return (
            f"Only {remaining} turn(s) remain before this Plan FAILS and the whole "
            'run aborts. Stop re-verifying: reply exactly {"decision":"submit",'
            '"reason":"..."} as soon as the required artifacts are in place, and '
            "reply abandon only if no acceptable version is achievable."
        )
    return (
        f"{remaining} turn(s) remain. Reply submit as soon as the required "
        "artifacts are in place instead of spending the budget on more checks."
    )


def search_retry_note(*, rejected_rounds: int) -> str:
    """Escalating note about the candidate-quality retry allowance.

    A guard rejection costs a whole turn and, unlike a scoring failure, is
    always the Agent's own doing: it either changed nothing, or changed
    something the frozen command never runs.  The note is delivered at the
    start of the *next* turn and only escalates once the allowance is nearly
    spent.
    """

    remaining = max(0, REJECTION_RETRY_LIMIT - rejected_rounds)
    if rejected_rounds == 0 or remaining > 2:
        return ""
    return (
        f"\n\nOnly {remaining} rejected-turn allowance(s) remain before this "
        "candidate is settled as a failure. A rejected turn is one that changed "
        "nothing measurable: either no source file changed at all, or the change "
        "never reaches the command the frozen manifest runs. Land the "
        "implementation in that command's path now."
    )

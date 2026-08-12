"""Durable contracts and shared execution helpers for research Plans."""

import asyncio
from collections.abc import Awaitable, Callable

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
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, CommitHash
from athena.core.research_models import Hypothesis

_TERMINAL_EVENT_KINDS = {"turn_completed", "turn_failed", "turn_interrupted"}

PublishEvent = Callable[[str, str, dict | None], Awaitable[None] | None]


async def forward_run_events(
    agents: AgentRuntime, run_id: str, publish: PublishEvent
) -> None:
    """Forward one Agent run's journal until its terminal event."""
    async for event in agents.run_events(run_id, after_sequence=0):
        await publish(event.kind, event.event_ref, event.data)
        if event.kind in _TERMINAL_EVENT_KINDS:
            return


async def wait_run_events(
    agents: AgentRuntime,
    run_id: str,
    publish: PublishEvent | None,
):
    """Wait for an Agent run while forwarding its journal when requested."""
    if publish is None:
        return await agents.wait_run(run_id)
    events = asyncio.create_task(forward_run_events(agents, run_id, publish))
    wait = asyncio.create_task(agents.wait_run(run_id))
    try:
        summary = await wait
        await events
        return summary
    finally:
        for task in (events, wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(events, wait, return_exceptions=True)


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
    evaluator_ref: ArtifactRef
    tree_ref: ArtifactRef
    human_context: str = ""
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

"""Durable clarification draft schemas.

These models are the canonical pre-confirmation source of truth. They are
deliberately separate from ``ResearchState.task_understanding``, which only
contains the confirmed run contract after ``confirm_and_start`` writes it.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.core.human_request import HumanRequest


class DraftUnderstanding(BaseModel):
    """The controller's best current structured understanding of a task."""

    model_config = ConfigDict(extra="forbid")

    title: str
    dataset: str | None = None
    target: str | None = None
    task_type: Literal["classification", "regression", "ranking", "other"]
    primary_metric: str | None = None
    direction: Literal["maximize", "minimize"] | None = None
    evaluation_plan: str | None = None


class ClarificationAnswer(BaseModel):
    """One settled question/outcome in the clarification Q&A."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    question: str
    outcome: Literal["choice", "text", "skip", "timeout", "cancelled"]
    value: str | None = None
    choice_label: str | None = None
    answered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UnresolvedItem(BaseModel):
    """One field that remains unknown or unconfirmed in the draft."""

    model_config = ConfigDict(extra="forbid")

    field: str
    reason: str
    critical: bool


class ClarificationFailure(BaseModel):
    """Retryable failure metadata for a FAILED draft."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool


class ClarificationRevision(BaseModel):
    """Durable evidence that a human asked for a revision."""

    model_config = ConfigDict(extra="forbid")

    base_revision: int
    instruction: str
    requested_at: datetime


class ClarificationDraft(BaseModel):
    """The persisted, versioned clarification draft for one session."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    draft_id: str
    revision: int = Field(ge=0)
    session_id: str
    original_task: str
    status: Literal[
        "CLARIFYING", "READY_FOR_CONFIRMATION", "CONFIRMED", "CANCELLED", "FAILED"
    ]
    understanding: DraftUnderstanding
    answers: list[ClarificationAnswer] = Field(default_factory=list)
    revisions: list[ClarificationRevision] = Field(default_factory=list)
    unresolved: list[UnresolvedItem] = Field(default_factory=list)
    pending_request: HumanRequest | None = None
    failure: ClarificationFailure | None = None
    questions_asked: int = Field(ge=0, le=8)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _validate_consistency(self) -> "ClarificationDraft":
        if self.pending_request is not None:
            if self.pending_request.session_id != self.session_id:
                raise ValueError(
                    "pending_request.session_id must match draft session_id"
                )
            if self.pending_request.scope_id != self.draft_id:
                raise ValueError("pending_request.scope_id must match draft_id")
            if self.pending_request.scope_kind != "clarification":
                raise ValueError("pending_request.scope_kind must be clarification")
            if self.status in {"READY_FOR_CONFIRMATION", "CONFIRMED", "CANCELLED"}:
                raise ValueError(
                    "terminal/ready drafts cannot have a pending human request"
                )
        if self.status == "FAILED" and self.failure is None:
            raise ValueError("FAILED draft must carry a failure object")
        if self.status != "FAILED" and self.failure is not None:
            raise ValueError("non-FAILED draft cannot carry a failure object")
        return self


class ConfirmationJournal(BaseModel):
    """Persist the rollback snapshot for one confirmation transaction."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    transaction_id: str
    session_id: str
    draft_id: str
    revision: int
    phase: Literal["PREPARED", "COMMITTED"]
    previous_state_json: str | None
    previous_resume_json: str | None = None
    previous_draft_json: str
    previous_handoff_text: str | None
    handoff_ref: str | None = None


__all__ = [
    "ClarificationAnswer",
    "ClarificationDraft",
    "ClarificationFailure",
    "ClarificationRevision",
    "ConfirmationJournal",
    "DraftUnderstanding",
    "UnresolvedItem",
]

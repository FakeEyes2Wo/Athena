"""Pure clarification draft transitions."""

from datetime import datetime

from athena.core.human_request import HumanOutcome, HumanRequest
from athena.research.clarification.generator import ClarificationFinalStep
from athena.research.clarification.models import (
    ClarificationAnswer,
    ClarificationDraft,
    ClarificationFailure,
    ClarificationRevision,
    DraftUnderstanding,
    UnresolvedItem,
)
from athena.research.clarification.requirements import (
    canonical_unresolved,
    initial_task_understanding,
    resolved_field,
)

MAX_QUESTIONS = 8
_DRAFT_FIELDS = {
    "dataset",
    "target",
    "task_type",
    "primary_metric",
    "direction",
    "evaluation_plan",
}


def new_draft(
    task: str, session_id: str, draft_id: str, now: datetime
) -> ClarificationDraft:
    """Create the first truthful persisted state for a task."""
    understanding, unresolved = initial_task_understanding(task)
    return ClarificationDraft(
        draft_id=draft_id,
        revision=0,
        session_id=session_id,
        original_task=task,
        status="CLARIFYING",
        understanding=understanding,
        unresolved=unresolved,
        questions_asked=0,
        created_at=now,
        updated_at=now,
    )


def set_pending(
    draft: ClarificationDraft, request: HumanRequest, now: datetime
) -> ClarificationDraft:
    """Record a newly asked question before awaiting its answer."""
    if draft.questions_asked >= MAX_QUESTIONS:
        raise ValueError("cannot ask more than eight clarification questions")
    return _updated(
        draft,
        pending_request=request,
        questions_asked=draft.questions_asked + 1,
        updated_at=now,
    )


def settle_answer(
    draft: ClarificationDraft,
    request: HumanRequest,
    outcome: HumanOutcome,
    now: datetime,
    *,
    field: str | None = None,
) -> ClarificationDraft:
    """Persist one typed outcome and apply a valid field value."""
    answers = [
        *draft.answers,
        ClarificationAnswer(
            request_id=request.request_id,
            question=request.prompt,
            outcome=outcome.kind,
            value=outcome.value,
            choice_label=outcome.choice_label,
            answered_at=outcome.settled_at,
        ),
    ]
    understanding, unresolved = _apply_value(
        draft.understanding, draft.unresolved, field, outcome
    )
    return _updated(
        draft,
        answers=answers,
        understanding=understanding,
        unresolved=canonical_unresolved(understanding, unresolved),
        pending_request=None,
        questions_asked=max(draft.questions_asked, len(answers)),
        revision=draft.revision + 1,
        updated_at=now,
    )


def finalize(
    draft: ClarificationDraft, step: ClarificationFinalStep, now: datetime
) -> ClarificationDraft:
    """Build the reviewable draft with canonical unresolved fields."""
    prior = draft.unresolved
    if step.understanding.task_type != draft.understanding.task_type:
        prior = resolved_field(prior, "task_type")
    unresolved = canonical_unresolved(step.understanding, [*prior, *step.unresolved])
    return _updated(
        draft,
        status="READY_FOR_CONFIRMATION",
        understanding=step.understanding,
        unresolved=unresolved,
        pending_request=None,
        failure=None,
        revision=draft.revision + 1,
        updated_at=now,
    )


def best_final(draft: ClarificationDraft, now: datetime) -> ClarificationDraft:
    """Finalize from persisted evidence when the question budget is exhausted."""
    return finalize(
        draft,
        ClarificationFinalStep(
            kind="final",
            understanding=draft.understanding,
            unresolved=draft.unresolved,
        ),
        now,
    )


def fail(draft: ClarificationDraft, now: datetime) -> ClarificationDraft:
    """Expose generator or broker failure without losing settled evidence."""
    return _updated(
        draft,
        status="FAILED",
        pending_request=None,
        failure=ClarificationFailure(
            code="provider_failure",
            message="clarification generation failed",
            retryable=True,
        ),
        unresolved=canonical_unresolved(draft.understanding, draft.unresolved),
        updated_at=now,
    )


def retry(draft: ClarificationDraft, now: datetime) -> ClarificationDraft:
    """Clear retryable failure metadata and resume clarification."""
    return _updated(draft, status="CLARIFYING", failure=None, updated_at=now)


def revise(
    draft: ClarificationDraft, instruction: str, now: datetime
) -> ClarificationDraft:
    """Append a human revision instruction without discarding evidence."""
    revision = ClarificationRevision(
        base_revision=draft.revision,
        instruction=instruction,
        requested_at=now,
    )
    return _updated(
        draft,
        status="CLARIFYING",
        pending_request=None,
        failure=None,
        revisions=[*draft.revisions, revision],
        revision=draft.revision + 1,
        updated_at=now,
    )


def cancel(draft: ClarificationDraft, now: datetime) -> ClarificationDraft:
    """End an unconfirmed draft while retaining its audit trail."""
    return _updated(
        draft,
        status="CANCELLED",
        pending_request=None,
        failure=None,
        updated_at=now,
    )


def _apply_value(
    understanding: DraftUnderstanding,
    unresolved: list[UnresolvedItem],
    field: str | None,
    outcome: HumanOutcome,
) -> tuple[DraftUnderstanding, list[UnresolvedItem]]:
    value = outcome.value.strip() if outcome.value else None
    if (
        field not in _DRAFT_FIELDS
        or outcome.kind not in {"choice", "text"}
        or not value
        or value in {"later", "default"}
    ):
        return understanding, unresolved
    if field == "direction" and value not in {"maximize", "minimize"}:
        return understanding, unresolved
    if field == "task_type" and value not in {
        "classification",
        "regression",
        "ranking",
        "other",
    }:
        return understanding, unresolved
    payload = understanding.model_dump()
    payload[field] = value
    updated = DraftUnderstanding.model_validate(payload)
    return updated, resolved_field(unresolved, field)


def _updated(draft: ClarificationDraft, **changes: object) -> ClarificationDraft:
    payload = draft.model_dump()
    payload.update(changes)
    return ClarificationDraft.model_validate(payload)


__all__ = [
    "MAX_QUESTIONS",
    "best_final",
    "cancel",
    "fail",
    "finalize",
    "new_draft",
    "retry",
    "revise",
    "set_pending",
    "settle_answer",
]

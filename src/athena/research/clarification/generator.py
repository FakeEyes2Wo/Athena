"""Clarification generation contract and deterministic production policy."""

import inspect
from collections.abc import Awaitable
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.core.human_request import HumanChoice, validate_choices
from athena.research.clarification.models import (
    ClarificationDraft,
    DraftUnderstanding,
    UnresolvedItem,
)
from athena.research.clarification.requirements import required_critical_fields


class ClarificationQuestionStep(BaseModel):
    """One question returned by a clarification generator."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["question"]
    field: Literal[
        "dataset",
        "target",
        "task_type",
        "primary_metric",
        "direction",
        "evaluation_plan",
    ]
    prompt: str
    choices: list[HumanChoice]
    allow_custom: bool
    allow_skip: bool

    @model_validator(mode="after")
    def _valid_choices(self) -> "ClarificationQuestionStep":
        validate_choices(self.choices)
        return self


class ClarificationFinalStep(BaseModel):
    """The generator's best final synthesis."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["final"]
    understanding: DraftUnderstanding
    unresolved: list[UnresolvedItem]


ClarificationStep = Annotated[
    ClarificationQuestionStep | ClarificationFinalStep,
    Field(discriminator="kind"),
]


class ClarificationGenerator(Protocol):
    """Produce one validated question or final synthesis from a draft."""

    def next_step(
        self, draft: ClarificationDraft
    ) -> Awaitable[ClarificationStep] | ClarificationStep:
        """Return the next policy decision for the current evidence."""
        ...


_QUESTIONS = {
    "dataset": (
        "Which dataset should this task use?",
        [
            HumanChoice(label="Use the provided dataset", value="provided"),
            HumanChoice(label="I will specify it later", value="later"),
        ],
    ),
    "target": (
        "Which column is the prediction target?",
        [
            HumanChoice(label="I will specify it later", value="later"),
            HumanChoice(label="Use the obvious target column", value="default"),
        ],
    ),
    "primary_metric": (
        "Which metric should be the primary success metric?",
        [
            HumanChoice(label="Accuracy", value="accuracy"),
            HumanChoice(label="F1", value="f1"),
            HumanChoice(label="ROC-AUC", value="roc_auc"),
        ],
    ),
    "direction": (
        "Should the primary metric be maximized or minimized?",
        [
            HumanChoice(label="Maximize", value="maximize"),
            HumanChoice(label="Minimize", value="minimize"),
        ],
    ),
    "evaluation_plan": (
        "How should the result be evaluated?",
        [
            HumanChoice(label="Hold-out split", value="holdout"),
            HumanChoice(label="Cross-validation", value="cross_validation"),
        ],
    ),
}


class DeterministicClarificationGenerator:
    """Ask each task-type requirement once, then return current evidence."""

    def next_step(self, draft: ClarificationDraft) -> ClarificationStep:
        """Ask the next required field or return the accumulated evidence."""
        fields = required_critical_fields(draft.understanding.task_type)
        if draft.questions_asked < len(fields):
            field = fields[draft.questions_asked]
            prompt, choices = _QUESTIONS[field]
            return ClarificationQuestionStep(
                kind="question",
                field=field,
                prompt=prompt,
                choices=choices,
                allow_custom=True,
                allow_skip=True,
            )
        return ClarificationFinalStep(
            kind="final",
            understanding=_apply_latest_revision(draft),
            unresolved=draft.unresolved,
        )


def _apply_latest_revision(draft: ClarificationDraft) -> DraftUnderstanding:
    """Apply safe structured directives from the latest revision instruction."""
    if not draft.revisions:
        return draft.understanding
    instruction = draft.revisions[-1].instruction.casefold()
    payload = draft.understanding.model_dump()
    for token, metric in (
        ("accuracy", "accuracy"),
        ("f1", "f1"),
        ("roc-auc", "roc_auc"),
        ("roc_auc", "roc_auc"),
        ("precision", "precision"),
        ("recall", "recall"),
        ("rmse", "rmse"),
        ("mae", "mae"),
    ):
        if token in instruction:
            payload["primary_metric"] = metric
    for direction in ("maximize", "minimize"):
        if direction in instruction:
            payload["direction"] = direction
    for task_type in ("classification", "regression", "ranking"):
        if task_type in instruction:
            payload["task_type"] = task_type
    if "cross-validation" in instruction or "cross validation" in instruction:
        payload["evaluation_plan"] = "cross_validation"
    elif "hold-out" in instruction or "holdout" in instruction:
        payload["evaluation_plan"] = "holdout"
    return DraftUnderstanding.model_validate(payload)


async def generate_step(
    generator: ClarificationGenerator | Any, draft: ClarificationDraft
) -> ClarificationStep:
    """Invoke and validate one generator step at the policy boundary."""
    method = getattr(generator, "next_step", generator)
    result = method(draft)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, (ClarificationQuestionStep, ClarificationFinalStep)):
        return result
    if isinstance(result, dict) and result.get("kind") == "question":
        return ClarificationQuestionStep.model_validate(result)
    if isinstance(result, dict) and result.get("kind") == "final":
        return ClarificationFinalStep.model_validate(result)
    raise TypeError(f"generator returned {type(result).__name__}")


__all__ = [
    "ClarificationFinalStep",
    "ClarificationGenerator",
    "ClarificationQuestionStep",
    "ClarificationStep",
    "DeterministicClarificationGenerator",
    "generate_step",
]

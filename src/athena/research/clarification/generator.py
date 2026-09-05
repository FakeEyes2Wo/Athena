"""Clarification generation contract and deterministic production policy."""

import inspect
import logging
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from athena.core.human_request import HumanChoice, validate_choices
from athena.research.clarification.models import (
    ClarificationDraft,
    DraftUnderstanding,
    UnresolvedItem,
)
from athena.research.clarification.requirements import required_critical_fields

logger = logging.getLogger(__name__)

ProgressStage = Literal["analysis", "question", "synthesis"]
ProgressSinkStage = ProgressStage | Literal["failure"]
ProgressSource = Literal["agent", "tool"]


def normalize_public_summary(value: object) -> str:
    """Normalize text that is explicitly allowed to cross the public boundary."""
    if not isinstance(value, str):
        # Pydantic turns ValueError into a field ValidationError; TypeError escapes.
        raise ValueError("public summary must be a string")  # noqa: TRY004
    normalized = unicodedata.normalize("NFKC", value)
    printable = "".join(
        " " if char.isspace() else char
        for char in normalized
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(printable.split())


class PublicProgress(BaseModel):
    """A short, explicitly public clarification progress update."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    stage: ProgressStage
    summary: Annotated[str, Field(min_length=1, max_length=600)]

    @field_validator("summary", mode="before")
    @classmethod
    def _normalize_summary(cls, value: object) -> str:
        return normalize_public_summary(value)


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


class ClarificationModelOutput(BaseModel):
    """Strict model envelope returned by an LLM clarification generator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    public_update: PublicProgress
    step: ClarificationStep


@dataclass(frozen=True)
class ClarificationTurnResult:
    """Normalized result consumed by the clarification controller."""

    step: ClarificationStep
    public_update: PublicProgress | None = None


class PublicProgressSink(Protocol):
    """Publish one sanitized clarification progress event."""

    async def __call__(
        self,
        *,
        summary: str,
        stage: ProgressSinkStage,
        session_id: str,
        scope_id: str,
        source: ProgressSource,
        persist: bool,
    ) -> None: ...


class ClarificationGenerator(Protocol):
    """Produce one validated question or final synthesis from a draft."""

    def next_step(
        self, draft: ClarificationDraft
    ) -> (
        Awaitable[ClarificationStep | ClarificationModelOutput]
        | ClarificationStep
        | ClarificationModelOutput
    ):
        """Return the next policy decision for the current evidence."""
        ...


ClarificationGeneratorInput = (
    ClarificationGenerator
    | Callable[
        [ClarificationDraft],
        Awaitable[ClarificationStep | ClarificationModelOutput]
        | ClarificationStep
        | ClarificationModelOutput,
    ]
)


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
    metric = _directed_value(
        instruction,
        (
            ("accuracy", "accuracy"),
            ("f1", "f1"),
            ("roc-auc", "roc_auc"),
            ("roc_auc", "roc_auc"),
            ("precision", "precision"),
            ("recall", "recall"),
            ("rmse", "rmse"),
            ("mae", "mae"),
        ),
    )
    if metric is not None:
        payload["primary_metric"] = metric
    direction = _directed_value(
        instruction,
        (("maximize", "maximize"), ("minimize", "minimize")),
    )
    if direction is not None:
        payload["direction"] = direction
    task_type = _directed_value(
        instruction,
        (
            ("classification", "classification"),
            ("regression", "regression"),
            ("ranking", "ranking"),
        ),
    )
    if task_type is not None:
        payload["task_type"] = task_type
    evaluation_plan = _directed_value(
        instruction,
        (
            ("cross-validation", "cross_validation"),
            ("cross validation", "cross_validation"),
            ("hold-out", "holdout"),
            ("holdout", "holdout"),
        ),
    )
    if evaluation_plan is not None:
        payload["evaluation_plan"] = evaluation_plan
    return DraftUnderstanding.model_validate(payload)


def _directed_value(
    instruction: str,
    choices: tuple[tuple[str, str], ...],
) -> str | None:
    """Select one existing value from an explicit, non-negated directive."""
    hits = [
        (instruction.index(token), value)
        for token, value in choices
        if token in instruction
        and f"not {token}" not in instruction
        and f"instead of {token}" not in instruction
    ]
    if not hits:
        return None
    directive_ends = [
        index + len(prefix)
        for prefix in ("use ", "keep ", "add ", "prefer ", "switch to ", "change to ")
        if (index := instruction.find(prefix)) >= 0
    ]
    for start in sorted(directive_ends):
        directed = [hit for hit in hits if hit[0] >= start]
        if directed:
            return min(directed)[1]
    values = {value for _index, value in hits}
    return values.pop() if len(values) == 1 else None


async def generate_turn(
    generator: ClarificationGeneratorInput, draft: ClarificationDraft
) -> ClarificationTurnResult:
    """Invoke a generator and normalize its typed result."""
    method = getattr(generator, "next_step", generator)
    result = method(draft)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, ClarificationModelOutput):
        return ClarificationTurnResult(
            step=result.step, public_update=result.public_update
        )
    if isinstance(result, (ClarificationQuestionStep, ClarificationFinalStep)):
        return ClarificationTurnResult(step=result)
    raise TypeError(f"generator returned {type(result).__name__}")


async def publish_public_progress(
    sink: PublicProgressSink | None,
    *,
    summary: str,
    stage: ProgressSinkStage,
    session_id: str,
    scope_id: str,
    source: ProgressSource,
    persist: bool,
) -> None:
    """Best-effort display publication that never mutates canonical state."""
    if sink is None:
        return
    try:
        await sink(
            summary=summary,
            stage=stage,
            session_id=session_id,
            scope_id=scope_id,
            source=source,
            persist=persist,
        )
    except Exception:
        logger.exception("public progress sink failed")


__all__ = [
    "ClarificationFinalStep",
    "ClarificationGenerator",
    "ClarificationGeneratorInput",
    "ClarificationModelOutput",
    "ClarificationQuestionStep",
    "ClarificationStep",
    "ClarificationTurnResult",
    "DeterministicClarificationGenerator",
    "ProgressSinkStage",
    "ProgressSource",
    "ProgressStage",
    "PublicProgress",
    "PublicProgressSink",
    "generate_turn",
    "normalize_public_summary",
    "publish_public_progress",
]

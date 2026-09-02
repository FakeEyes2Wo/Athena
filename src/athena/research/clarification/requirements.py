"""Pure task-contract requirements and unresolved-field rules."""

import re
from collections.abc import Iterable

from athena.research.clarification.models import DraftUnderstanding, UnresolvedItem

_CRITICAL_FIELDS = {
    "classification": (
        "dataset",
        "target",
        "primary_metric",
        "direction",
        "evaluation_plan",
    ),
    "regression": (
        "dataset",
        "target",
        "primary_metric",
        "direction",
        "evaluation_plan",
    ),
    "ranking": (
        "dataset",
        "target",
        "primary_metric",
        "direction",
        "evaluation_plan",
    ),
    "other": ("dataset", "primary_metric", "direction", "evaluation_plan"),
}


def normalize_task(task: str) -> str:
    """Return the stable identity form used by start/resume conflict checks."""
    return " ".join(re.split(r"\s+", task.strip())).casefold()


def required_critical_fields(task_type: str) -> tuple[str, ...]:
    """Return fields that must be known or explicitly acknowledged."""
    return _CRITICAL_FIELDS.get(task_type, _CRITICAL_FIELDS["other"])


def initial_task_understanding(
    task: str,
) -> tuple[DraftUnderstanding, list[UnresolvedItem]]:
    """Create a truthful draft without metric or direction defaults."""
    title = " ".join(task.strip().split())[:120] or "Untitled task"
    understanding = DraftUnderstanding(title=title, task_type="other")
    unresolved = [
        UnresolvedItem(field="task_type", reason="not determined", critical=True)
    ]
    return understanding, canonical_unresolved(understanding, unresolved)


def canonical_unresolved(
    understanding: DraftUnderstanding,
    proposed: Iterable[UnresolvedItem] = (),
) -> list[UnresolvedItem]:
    """Deduplicate unresolved items and add every missing critical field."""
    required = required_critical_fields(understanding.task_type)
    items: dict[str, UnresolvedItem] = {}
    for item in proposed:
        if item.field in required and _is_known(understanding, item.field):
            continue
        items[item.field] = item
    for field in required:
        if not _is_known(understanding, field):
            previous = items.get(field)
            items[field] = UnresolvedItem(
                field=field,
                reason=previous.reason if previous else "not supplied",
                critical=True,
            )
    return list(items.values())


def resolved_field(
    unresolved: Iterable[UnresolvedItem], field: str
) -> list[UnresolvedItem]:
    """Remove stale unresolved metadata after a field receives a valid answer."""
    return [item for item in unresolved if item.field != field]


def _is_known(understanding: DraftUnderstanding, field: str) -> bool:
    value = getattr(understanding, field, None)
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


__all__ = [
    "canonical_unresolved",
    "initial_task_understanding",
    "normalize_task",
    "required_critical_fields",
    "resolved_field",
]

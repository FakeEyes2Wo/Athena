"""Critical-field policy tests."""

import pytest

from athena.research.clarification.models import DraftUnderstanding, UnresolvedItem
from athena.research.clarification.requirements import (
    canonical_unresolved,
    normalize_task,
    required_critical_fields,
)


def test_canonical_unresolved_adds_missing_and_removes_stale_fields() -> None:
    understanding = DraftUnderstanding(
        title="Churn",
        task_type="classification",
        dataset="churn.csv",
    )
    unresolved = canonical_unresolved(
        understanding,
        [UnresolvedItem(field="dataset", reason="old", critical=True)],
    )

    fields = {item.field for item in unresolved if item.critical}
    assert "dataset" not in fields
    assert {"target", "primary_metric", "direction", "evaluation_plan"} <= fields


@pytest.mark.parametrize(
    ("task_type", "has_target"),
    [
        ("classification", True),
        ("regression", True),
        ("ranking", True),
        ("other", False),
        ("unknown", False),
    ],
)
def test_required_fields_share_predictive_policy(
    task_type: str, has_target: bool
) -> None:
    fields = required_critical_fields(task_type)
    assert ("target" in fields) is has_target
    assert fields[0] == "dataset"
    assert fields[-1] == "evaluation_plan"


def test_normalize_task_collapses_all_whitespace() -> None:
    assert normalize_task("  PREDICT\tChurn\nNow  ") == "predict churn now"

"""Critical-field policy tests."""

from athena.research.clarification.models import DraftUnderstanding, UnresolvedItem
from athena.research.clarification.requirements import canonical_unresolved


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

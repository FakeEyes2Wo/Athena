from pathlib import Path

import pytest

from athena.core.research_models import TaskUnderstanding
from athena.research.rubrics.task import assess_task_readiness


def _understanding(dataset: str, **updates) -> TaskUnderstanding:
    values = {
        "title": "diagnosis",
        "dataset": dataset,
        "target": "diagnosis",
        "task_type": "classification",
        "evaluation_plan": "hold-out classification evaluation",
        "confidence": 0.8,
    }
    values.update(updates)
    return TaskUnderstanding(**values)


def test_complete_local_table_is_ready(tmp_path: Path) -> None:
    dataset = tmp_path / "medical.csv"
    dataset.write_text("feature,diagnosis\n1,benign\n", encoding="utf-8")

    result = assess_task_readiness(_understanding(str(dataset)), project_root=tmp_path)

    assert result.readiness == "READY"
    assert result.missing_items == []
    assert result.primary_metric is None
    assert any("Layer 1" in warning for warning in result.warnings)


def test_missing_dataset_and_target_produce_actionable_questions(
    tmp_path: Path,
) -> None:
    result = assess_task_readiness(_understanding("", target=""), project_root=tmp_path)

    assert result.readiness == "NEEDS_INPUT"
    assert {item.field for item in result.missing_items} == {"dataset", "target"}
    assert len(result.clarification_questions) == 2


def test_missing_local_path_is_critical(tmp_path: Path) -> None:
    result = assess_task_readiness(
        _understanding("data/missing.csv"), project_root=tmp_path
    )

    assert result.readiness == "NEEDS_INPUT"
    assert result.missing_items[0].field == "dataset"


def test_absent_target_column_is_critical(tmp_path: Path) -> None:
    dataset = tmp_path / "medical.csv"
    dataset.write_text("feature,label\n1,benign\n", encoding="utf-8")

    result = assess_task_readiness(_understanding(str(dataset)), project_root=tmp_path)

    assert result.readiness == "NEEDS_INPUT"
    assert any(item.field == "target" for item in result.missing_items)


def test_utf8_bom_table_header_is_supported(tmp_path: Path) -> None:
    dataset = tmp_path / "medical.csv"
    dataset.write_text("feature,diagnosis\n1,benign\n", encoding="utf-8-sig")

    result = assess_task_readiness(_understanding(str(dataset)), project_root=tmp_path)

    assert result.readiness == "READY"


def test_resolved_metric_requires_matching_provenance_field() -> None:
    with pytest.raises(ValueError, match="human_primary_metric"):
        TaskUnderstanding(
            title="task",
            dataset="named dataset",
            target="label",
            task_type="classification",
            primary_metric="f1",
            direction="maximize",
            metric_source="human",
        )


def test_resolved_direction_requires_matching_provenance_field() -> None:
    with pytest.raises(ValueError, match="official_direction"):
        TaskUnderstanding(
            title="task",
            dataset="named dataset",
            target="label",
            task_type="classification",
            primary_metric="roc_auc",
            direction="maximize",
            metric_source="official",
            official_primary_metric="roc_auc",
        )

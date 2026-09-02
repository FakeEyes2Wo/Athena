"""Focused contracts for the function-specific PREPARE modules."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.prepare.baseline import directory_candidate_task
from athena.research.prepare.data import prepare_platform_split
from athena.research.prepare.eda import usable_eda_reports, write_fallback_eda
from athena.research.prepare.evaluator import evaluator_tasks


async def _noop_publish(**_kwargs) -> None:
    """Stand in for runtime output publishing."""


@pytest.mark.asyncio
async def test_prepare_data_skips_non_csv_contracts(tmp_path: Path) -> None:
    """Directory data must retain the evaluator-owned split path."""
    runtime = SimpleNamespace(
        config=SimpleNamespace(dataset_path=None, target_column=None),
        workspaces_root=tmp_path,
        publish_output=_noop_publish,
    )

    assert await prepare_platform_split(runtime) is None


def test_directory_evaluator_tasks_keep_csv_filenames_out(tmp_path: Path) -> None:
    """Directory evaluators receive deterministic SEARCH and FINAL rules."""
    runtime = SimpleNamespace(workspaces_root=tmp_path)

    search, final = evaluator_tasks(runtime, "evaluate images")

    assert "SEARCH partition" in search
    assert "FINAL partition" in final
    assert "SHA-256" in search
    assert "final_labels.csv in the platform" not in final


def test_directory_evaluators_share_one_label_independent_identity_contract(
    tmp_path: Path,
) -> None:
    """Both roles derive dataset identities without leaking labels into them."""
    runtime = SimpleNamespace(workspaces_root=tmp_path)

    search, final = evaluator_tasks(runtime, "evaluate images")

    for task in (search, final):
        assert "dataset's actual identity" in task
        assert "independent of partition membership and labels" in task
        assert "label-derived directory segments" in task
    assert "Do not read, copy, derive, or disclose FINAL labels" in search


def test_directory_evaluators_declare_flexible_prediction_schema(
    tmp_path: Path,
) -> None:
    """Both evaluator roles declare fields without fixing one dataset's schema."""
    runtime = SimpleNamespace(workspaces_root=tmp_path)

    search, final = evaluator_tasks(runtime, "evaluate images")

    for task in (search, final):
        assert "prediction_id_column" in task
        assert "prediction_column" in task
        assert "probability_columns" in task
        assert "do not assume JW-SSD labels or class names" in task


def test_eda_fallback_preserves_existing_handoff(tmp_path: Path) -> None:
    """Fallback creation must not overwrite a useful EDA handoff."""
    handoff = tmp_path / "EDA_HANDOFF.md"
    handoff.write_text("# useful\n", encoding="utf-8")

    write_fallback_eda(tmp_path)

    assert handoff.read_text(encoding="utf-8") == "# useful\n"
    assert usable_eda_reports(tmp_path) == []


def test_directory_candidate_reads_the_runtime_split() -> None:
    """Directory candidates select search or final data at execution time."""
    task = directory_candidate_task("train a model")

    assert "ATHENA_EVALUATION_SPLIT" in task
    assert "'search'" in task
    assert "'final'" in task

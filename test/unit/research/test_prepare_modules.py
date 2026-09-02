"""Focused contracts for the function-specific PREPARE modules."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.prepare import orchestrator
from athena.research.prepare.baseline import directory_candidate_task
from athena.research.prepare.baseline_research import BaselineResearchError
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


@pytest.mark.asyncio
async def test_prepare_phase_threads_verified_research_into_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = object()
    captured: dict[str, object] = {}
    workspace = SimpleNamespace(path=str(tmp_path))
    runtime = SimpleNamespace(
        prepare_phase=None,
        provider=object(),
        task_text="task",
    )

    async def fake_prepare_baseline_design(*_args, **_kwargs):
        return verified

    async def fake_run_baseline(*args):
        captured["verified"] = args[-1]
        return "prepared"

    monkeypatch.setattr(
        orchestrator, "confirmed_task_context_block", lambda _runtime: _async("")
    )
    monkeypatch.setattr(
        orchestrator, "prepare_workspace", lambda _runtime: _async(workspace)
    )
    monkeypatch.setattr(
        orchestrator, "prepare_platform_split", lambda _runtime: _async(None)
    )
    monkeypatch.setattr(
        orchestrator,
        "prepare_evaluators",
        lambda *_args: _async(SimpleNamespace(search_ref="eval")),
    )
    monkeypatch.setattr(orchestrator, "prepare_eda", lambda *_args: _async(True))
    monkeypatch.setattr(
        orchestrator, "prepare_baseline_design", fake_prepare_baseline_design
    )
    monkeypatch.setattr(orchestrator, "run_baseline", fake_run_baseline)

    result = await orchestrator.run_prepare_phase(runtime, object())

    assert result == "prepared"
    assert captured["verified"] is verified


@pytest.mark.asyncio
async def test_prepare_phase_never_runs_baseline_after_research_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = SimpleNamespace(path=str(tmp_path))
    runtime = SimpleNamespace(
        prepare_phase=None,
        provider=object(),
        task_text="task",
    )

    async def fail_research(*_args, **_kwargs):
        raise BaselineResearchError("research rejected")

    async def forbidden_baseline(*_args, **_kwargs):
        raise AssertionError("run_baseline must not run after research failure")

    monkeypatch.setattr(
        orchestrator, "confirmed_task_context_block", lambda _runtime: _async("")
    )
    monkeypatch.setattr(
        orchestrator, "prepare_workspace", lambda _runtime: _async(workspace)
    )
    monkeypatch.setattr(
        orchestrator, "prepare_platform_split", lambda _runtime: _async(None)
    )
    monkeypatch.setattr(
        orchestrator,
        "prepare_evaluators",
        lambda *_args: _async(SimpleNamespace(search_ref="eval")),
    )
    monkeypatch.setattr(orchestrator, "prepare_eda", lambda *_args: _async(True))
    monkeypatch.setattr(orchestrator, "prepare_baseline_design", fail_research)
    monkeypatch.setattr(orchestrator, "run_baseline", forbidden_baseline)

    with pytest.raises(BaselineResearchError, match="research rejected"):
        await orchestrator.run_prepare_phase(runtime, object())


async def _async(value):
    return value

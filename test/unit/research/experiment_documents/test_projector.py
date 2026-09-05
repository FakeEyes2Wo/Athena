"""Tests for the safe experiment-document projection facade."""

import hashlib
import json
import logging
from pathlib import Path

import pytest

from athena.core.research_tree import ResearchTree
import athena.research.experiment_documents.projector as projector_module
import athena.research.experiment_documents.store as store_module
from athena.research.experiment_documents import (
    ExperimentDocumentProjector,
    ProjectionOutcome,
)
from athena.research.experiment_documents.store import RunDocumentConflict


@pytest.fixture
def research_tree() -> ResearchTree:
    return ResearchTree()


def valid_search_event() -> dict[str, object]:
    return {
        "run_id": "exp_search-1",
        "stage": "search",
        "status": "SUCCEEDED",
        "metric": {
            "primary": 0.81,
            "reference": 0.78,
            "generalization_gap": None,
            "secondary": {"f1": 0.79},
        },
        "artifacts": {"evidence": "sha256:evidence"},
        "reason": {"kind": "trusted_score", "summary": "Trusted score won."},
        "provenance": {
            "experiment_id": "exp_search-1",
            "hypothesis_id": "search-1",
            "commit": "abc123",
        },
    }


def test_project_stage_writes_archive_alias_reports_and_valid_manifest(
    tmp_path: Path, research_tree: ResearchTree
) -> None:
    evaluator = tmp_path / "workspaces" / "evaluator"
    evaluator.mkdir(parents=True)
    (evaluator / "metric.json").write_text(
        json.dumps({"primary_metric": "frozen_score"}), encoding="utf-8"
    )
    projector = ExperimentDocumentProjector(
        tmp_path / ".athena" / "exp_docs",
        (evaluator / "evaluate", evaluator),
    )

    outcome = projector.project_stage(
        valid_search_event(),
        tree=research_tree,
        validation=None,
        validation_skipped=False,
        task_understanding={"primary_metric": "f1"},
        direction="maximize",
    )

    assert outcome.ok is True
    root = tmp_path / ".athena" / "exp_docs"
    assert json.loads((root / "search.json").read_text())["metric"]["name"] == (
        "frozen_score"
    )
    assert (root / "runs" / "exp_search-1.json").is_file()
    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    for relative, digest in latest["files"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest


def test_invalid_event_returns_only_sanitized_failure_and_writes_nothing(
    tmp_path: Path,
    research_tree: ResearchTree,
    caplog: pytest.LogCaptureFixture,
) -> None:
    projector = ExperimentDocumentProjector(tmp_path / "docs", (tmp_path / "missing",))

    with caplog.at_level(logging.WARNING):
        outcome = projector.project_stage(
            {**valid_search_event(), "run_id": "../escape"},
            tree=research_tree,
            validation=None,
            validation_skipped=False,
            task_understanding=None,
            direction="maximize",
        )

    assert outcome == ProjectionOutcome.stale()
    assert not (tmp_path / "docs").exists()
    assert "../escape" not in outcome.warning_message
    assert "document projection failed" in caplog.text


def test_store_conflict_returns_stale_without_exposing_exception(
    tmp_path: Path, research_tree: ResearchTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    projector = ExperimentDocumentProjector(tmp_path / "docs", ())

    def fail(_batch: object) -> str:
        raise RunDocumentConflict("internal target path")

    monkeypatch.setattr(projector._store, "commit", fail)
    outcome = projector.project_stage(
        valid_search_event(),
        tree=research_tree,
        validation=None,
        validation_skipped=False,
        task_understanding=None,
        direction="maximize",
    )

    assert outcome == ProjectionOutcome.stale()
    assert "internal target path" not in outcome.warning_message


def test_replace_failure_returns_fixed_stale_without_leaking_error(
    tmp_path: Path,
    research_tree: ResearchTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projector = ExperimentDocumentProjector(tmp_path / "docs", ())

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("secret-target-path")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    outcome = projector.project_stage(
        valid_search_event(),
        tree=research_tree,
        validation=None,
        validation_skipped=False,
        task_understanding=None,
        direction="maximize",
    )

    assert outcome == ProjectionOutcome.stale()
    assert "secret-target-path" not in outcome.warning_message


def test_projection_snapshots_inputs_before_store_callback_mutates_them(
    tmp_path: Path,
    research_tree: ResearchTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation: dict[str, object] = {"final_test_score": 0.11}
    task_understanding: dict[str, object] = {"primary_metric": "before"}
    projector = ExperimentDocumentProjector(tmp_path / "docs", ())
    captured: dict[str, object] = {}

    original_render = projector_module.render_final_report

    def capture_render(
        tree: ResearchTree,
        rendered_validation: object,
        *,
        validation_skipped: bool,
    ) -> bytes:
        captured["tree"] = tree
        captured["validation"] = rendered_validation
        research_tree._sota_id = "mutated-tree"
        validation["final_test_score"] = 0.99
        return original_render(
            tree, rendered_validation, validation_skipped=validation_skipped
        )

    def capture_metric(task: object) -> str:
        captured["task"] = task
        task_understanding["primary_metric"] = "after"
        return task["primary_metric"]  # type: ignore[index]

    monkeypatch.setattr(projector_module, "render_final_report", capture_render)
    monkeypatch.setattr(projector._metrics, "resolve", capture_metric)
    outcome = projector.project_stage(
        valid_search_event(),
        tree=research_tree,
        validation=validation,
        validation_skipped=False,
        task_understanding=task_understanding,
        direction="maximize",
    )

    assert outcome.ok is True
    assert captured["tree"] is not research_tree
    assert captured["validation"] is not validation
    assert captured["validation"]["final_test_score"] == 0.11  # type: ignore[index]
    assert captured["tree"]._sota_id is None  # type: ignore[union-attr]
    assert captured["task"] is not task_understanding
    assert captured["task"]["primary_metric"] == "before"  # type: ignore[index]
    assert research_tree._sota_id == "mutated-tree"
    assert validation["final_test_score"] == 0.99
    assert task_understanding["primary_metric"] == "after"


def test_rebuild_is_safe_until_rebuild_facade_is_implemented(
    tmp_path: Path, research_tree: ResearchTree
) -> None:
    projector = ExperimentDocumentProjector(tmp_path / "docs", ())

    outcome = projector.rebuild(
        tree=research_tree,
        validation=None,
        validation_skipped=True,
        task_understanding=None,
        direction="maximize",
    )

    assert outcome == ProjectionOutcome.stale()

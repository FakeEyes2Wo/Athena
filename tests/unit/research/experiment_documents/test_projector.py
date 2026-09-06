"""Tests for the safe experiment-document projection facade."""

import hashlib
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import athena.research.experiment_documents.projector as projector_module
import athena.research.experiment_documents.store as store_module
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.experiment_documents import (
    ExperimentDocumentProjector,
    ProjectionContext,
)
from athena.research.experiment_documents.store import RunDocumentConflict


@pytest.fixture
def research_tree() -> ResearchTree:
    return ResearchTree()


def context(
    tree: ResearchTree,
    validation: dict[str, object] | None = None,
    *,
    skipped: bool = False,
    task: dict[str, object] | None = None,
    direction: str = "maximize",
) -> ProjectionContext:
    return ProjectionContext(
        tree=tree,
        state=SimpleNamespace(
            validation=validation,
            validation_skipped=skipped,
            task_understanding=task,
        ),
        direction=direction,  # type: ignore[arg-type]
    )


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


def test_projector_boundary_has_one_context_parameter() -> None:
    assert tuple(inspect.signature(ExperimentDocumentProjector.project).parameters) == (
        "self",
        "event",
        "context",
    )
    assert tuple(inspect.signature(ExperimentDocumentProjector.rebuild).parameters) == (
        "self",
        "context",
    )
    assert not hasattr(ExperimentDocumentProjector, "project_stage")


def test_project_writes_archive_alias_reports_and_valid_manifest(
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

    outcome = projector.project(
        valid_search_event(),
        context(research_tree, task={"primary_metric": "f1"}),
    )

    assert outcome is True
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
        outcome = projector.project(
            {**valid_search_event(), "run_id": "../escape"},
            context(research_tree),
        )

    assert outcome is False
    assert not (tmp_path / "docs").exists()
    assert "document projection failed" in caplog.text


def test_store_conflict_returns_stale_without_exposing_exception(
    tmp_path: Path, research_tree: ResearchTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    projector = ExperimentDocumentProjector(tmp_path / "docs", ())

    def fail(_batch: object) -> str:
        raise RunDocumentConflict("internal target path")

    monkeypatch.setattr(projector._store, "commit", fail)
    outcome = projector.project(
        valid_search_event(),
        context(research_tree),
    )

    assert outcome is False


def test_replace_failure_returns_fixed_stale_without_leaking_error(
    tmp_path: Path,
    research_tree: ResearchTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projector = ExperimentDocumentProjector(tmp_path / "docs", ())

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("secret-target-path")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    outcome = projector.project(
        valid_search_event(),
        context(research_tree),
    )

    assert outcome is False


def test_projection_snapshots_inputs_before_store_callback_mutates_them(
    tmp_path: Path,
    research_tree: ResearchTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation: dict[str, object] = {"final_test_score": 0.11}
    task_understanding: dict[str, object] = {"primary_metric": "before"}
    projector = ExperimentDocumentProjector(tmp_path / "docs", ())
    captured: dict[str, object] = {}

    original_render = projector_module.build_final_report

    def capture_render(
        tree: ResearchTree,
        rendered_validation: object,
        *,
        validation_skipped: bool,
    ) -> str:
        captured["tree"] = tree
        captured["validation"] = rendered_validation
        research_tree._sota_id = "mutated-tree"
        validation["final_test_score"] = 0.99
        return original_render(
            tree, rendered_validation, validation_skipped=validation_skipped
        )

    def capture_metric(_roots: object, task: object) -> str:
        captured["task"] = task
        task_understanding["primary_metric"] = "after"
        return task["primary_metric"]  # type: ignore[index]

    monkeypatch.setattr(projector_module, "build_final_report", capture_render)
    monkeypatch.setattr(projector_module, "_resolve_metric", capture_metric)
    outcome = projector.project(
        valid_search_event(),
        context(research_tree, validation, task=task_understanding),
    )

    assert outcome is True
    assert captured["tree"] is not research_tree
    assert captured["validation"] is not validation
    assert captured["validation"]["final_test_score"] == 0.11  # type: ignore[index]
    assert captured["tree"]._sota_id is None  # type: ignore[union-attr]
    assert captured["task"] is not task_understanding
    assert captured["task"]["primary_metric"] == "before"  # type: ignore[index]
    assert research_tree._sota_id == "mutated-tree"
    assert validation["final_test_score"] == 0.99
    assert task_understanding["primary_metric"] == "after"


def projector_for(tmp_path: Path) -> ExperimentDocumentProjector:
    return ExperimentDocumentProjector(
        tmp_path / ".athena" / "exp_docs", (tmp_path / "missing",)
    )


def _completed_tree() -> ResearchTree:
    tree = ResearchTree()
    for index, (experiment_id, kind, status) in enumerate(
        (
            ("exp_baseline", "baseline", ExperimentStatus.SUCCEEDED),
            ("exp_search-1", "search", ExperimentStatus.SUCCEEDED),
            ("exp_search-2", "search", ExperimentStatus.SUCCEEDED),
            ("exp_running", "search", ExperimentStatus.RUNNING),
        )
    ):
        hypothesis_id = f"hyp-{index}"
        tree.add_hypothesis(
            Hypothesis(
                id=hypothesis_id,
                statement=f"statement {index}",
                intervention=f"intervention {index}",
                expected_effect=f"effect {index}",
            )
        )
        tree.add_experiment(
            experiment_id,
            Experiment(
                hypothesis_id=hypothesis_id,
                commit=f"commit-{index}",
                plan=ExperimentPlan(
                    kind=kind,
                    change=f"change {index}",
                    run_config_ref=f"config-{index}",
                    budget={},
                    acceptance_rule="score",
                ),
                gitwork=GitWorkBranch(
                    path=f"worktree-{index}",
                    branch=f"branch-{index}",
                    base_commit=f"base-{index}",
                ),
                status=status,
                eval=(
                    EvalResult(
                        experiment_id=experiment_id,
                        primary=0.70 + index / 100,
                        secondary={"f1": 0.60 + index / 100},
                        per_sample=f"evidence-{index}",
                    )
                    if status is ExperimentStatus.SUCCEEDED
                    else None
                ),
                error=None,
            ),
        )
    tree.set_sota("exp_search-2")
    return tree


@pytest.fixture
def completed_tree() -> ResearchTree:
    return _completed_tree()


@pytest.fixture
def validation() -> dict[str, object]:
    return {
        "status": "COMPLETED",
        "result_id": "validation-1",
        "test_score": 0.81,
        "final_test_score": 0.79,
        "generalization_gap": 0.02,
        "predictions_ref": "predictions-1",
        "report_ref": "report-1",
        "validation_commit": "validate-commit",
    }


def test_rebuild_restores_all_derivable_runs_and_latest_aliases(
    tmp_path: Path, completed_tree: ResearchTree, validation: dict[str, object]
) -> None:
    root = tmp_path / ".athena" / "exp_docs"
    unknown = root / "runs" / "historical-unknown.json"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b'{"opaque": true}\n')

    outcome = projector_for(tmp_path).rebuild(
        context(completed_tree, validation, task={"primary_metric": "accuracy"})
    )

    assert outcome is True
    assert unknown.read_bytes() == b'{"opaque": true}\n'
    assert (root / "runs" / "exp_baseline.json").is_file()
    assert (root / "runs" / "exp_search-1.json").is_file()
    assert (root / "runs" / "exp_search-2.json").is_file()
    assert not (root / "runs" / "exp_running.json").exists()
    assert json.loads((root / "baseline.json").read_text())["run_id"] == "exp_baseline"
    assert json.loads((root / "search.json").read_text())["run_id"] == "exp_search-2"
    assert json.loads((root / "final.json").read_text())["run_id"] == "validation-1"
    assert json.loads((root / "latest.json").read_text())["kind"] == "rebuild"


@pytest.mark.parametrize("status", ["PENDING", "RUNNING"])
def test_rebuild_excludes_nonterminal_experiments(tmp_path: Path, status: str) -> None:
    tree = _completed_tree()
    payload = tree.to_dict()
    payload["experiments"]["exp_search-1"]["status"] = status
    payload["experiments"]["exp_search-1"]["eval"] = None
    rebuilt_tree = ResearchTree.from_dict(payload)

    outcome = projector_for(tmp_path).rebuild(
        context(rebuilt_tree, task={"primary_metric": "accuracy"})
    )

    assert outcome is True
    assert not (
        tmp_path / ".athena" / "exp_docs" / "runs" / "exp_search-1.json"
    ).exists()


def test_rebuild_final_requires_terminal_validation_identity(
    tmp_path: Path, completed_tree: ResearchTree
) -> None:
    root = tmp_path / ".athena" / "exp_docs"
    projector = projector_for(tmp_path)
    checkpoint = {"status": "RUNNING", "result_ref": "pending-result"}
    assert (
        projector.rebuild(
            context(completed_tree, checkpoint, task={"primary_metric": "accuracy"})
        )
        is not None
    )
    assert not (root / "final.json").exists()

    legacy = {"status": "SUCCEEDED", "test_score": 0.72, "final_test_score": 0.70}
    assert (
        projector.rebuild(
            context(completed_tree, legacy, task={"primary_metric": "accuracy"})
        )
        is not None
    )
    assert json.loads((root / "final.json").read_text())["run_id"] == "final"


def test_rebuild_final_prefers_persisted_validation_sota_commit(
    tmp_path: Path,
    completed_tree: ResearchTree,
    validation: dict[str, object],
) -> None:
    validation["sota_commit"] = "persisted-sota-commit"

    outcome = projector_for(tmp_path).rebuild(
        context(completed_tree, validation, task={"primary_metric": "accuracy"})
    )

    assert outcome is True
    final = json.loads((tmp_path / ".athena" / "exp_docs" / "final.json").read_text())
    assert final["provenance"]["sota_commit"] == "persisted-sota-commit"


def test_rebuild_skipped_validation_uses_sota_without_metrics(
    tmp_path: Path, completed_tree: ResearchTree
) -> None:
    root = tmp_path / ".athena" / "exp_docs"
    outcome = projector_for(tmp_path).rebuild(
        context(
            completed_tree,
            skipped=True,
            task={"primary_metric": "accuracy"},
        )
    )
    assert outcome is True
    final = json.loads((root / "final.json").read_text())
    assert final["run_id"] == "final-skipped"
    assert final["metric"]["primary"] is None
    assert final["metric"]["reference"] == 0.72
    assert final["metric"]["generalization_gap"] is None
    assert final["metric"]["secondary"] == {}
    assert "final_test_score" not in (root / "FINAL_REPORT.md").read_text(
        encoding="utf-8"
    )


def test_rebuild_retains_enriched_compatible_run_bytes(
    tmp_path: Path, completed_tree: ResearchTree
) -> None:
    projector = projector_for(tmp_path)
    assert (
        projector.rebuild(context(completed_tree, task={"primary_metric": "accuracy"}))
        is not None
    )
    run_path = tmp_path / ".athena" / "exp_docs" / "runs" / "exp_search-1.json"
    payload = json.loads(run_path.read_text())
    payload["reason"]["summary"] = "enriched historical explanation"
    run_path.write_text(json.dumps(payload, indent=4) + "\n")
    enriched = run_path.read_bytes()

    assert (
        projector.rebuild(context(completed_tree, task={"primary_metric": "accuracy"}))
        is not None
    )
    assert run_path.read_bytes() == enriched


def test_rebuild_accepts_archives_written_by_normal_stage_projection(
    tmp_path: Path, completed_tree: ResearchTree
) -> None:
    projector = projector_for(tmp_path)
    tree_payload = completed_tree.to_dict()
    tree_payload["experiments"]["exp_baseline"]["eval"]["secondary"] = {}
    completed_tree = ResearchTree.from_dict(tree_payload)
    baseline = completed_tree.get_experiment("exp_baseline")
    assert (
        projector.project(
            {
                "run_id": "exp_baseline",
                "stage": "baseline",
                "status": baseline.status.value,
                "metric": {"primary": baseline.eval.primary},
                "artifacts": baseline.artifacts,
                "reason": {"kind": "trusted_score", "summary": "trusted baseline"},
                "provenance": {
                    "experiment_id": "exp_baseline",
                    "hypothesis_id": baseline.hypothesis_id,
                    "commit": baseline.commit,
                },
            },
            context(completed_tree, task={"primary_metric": "accuracy"}),
        )
        is not None
    )
    archive = (
        tmp_path / ".athena" / "exp_docs" / "runs" / "exp_baseline.json"
    ).read_bytes()

    outcome = projector.rebuild(
        context(completed_tree, task={"primary_metric": "accuracy"})
    )

    assert outcome is True
    assert (
        tmp_path / ".athena" / "exp_docs" / "runs" / "exp_baseline.json"
    ).read_bytes() == archive


def test_rebuild_accepts_final_archive_and_normalizes_artifact_keys(
    tmp_path: Path, completed_tree: ResearchTree, validation: dict[str, object]
) -> None:
    projector = projector_for(tmp_path)
    assert (
        projector.project(
            {
                "run_id": validation["result_id"],
                "stage": "final",
                "status": validation["status"],
                "metric": {
                    "primary": validation["final_test_score"],
                    "reference": validation["test_score"],
                    "generalization_gap": validation["generalization_gap"],
                },
                "artifacts": {
                    "predictions": validation["predictions_ref"],
                    "report": validation["report_ref"],
                },
                "reason": {"kind": "generalizes", "summary": "validated result"},
                "provenance": {
                    "sota_experiment_id": "exp_search-2",
                    "sota_commit": completed_tree.get_experiment("exp_search-2").commit,
                    "validation_commit": validation["validation_commit"],
                },
            },
            context(completed_tree, validation, task={"primary_metric": "accuracy"}),
        )
        is not None
    )
    archive = (
        tmp_path / ".athena" / "exp_docs" / "runs" / "validation-1.json"
    ).read_bytes()

    outcome = projector.rebuild(
        context(completed_tree, validation, task={"primary_metric": "accuracy"})
    )

    assert outcome is True
    assert (
        tmp_path / ".athena" / "exp_docs" / "runs" / "validation-1.json"
    ).read_bytes() == archive


def test_rebuild_conflict_preserves_run_alias_and_manifest(
    tmp_path: Path, completed_tree: ResearchTree
) -> None:
    projector = projector_for(tmp_path)
    assert (
        projector.rebuild(context(completed_tree, task={"primary_metric": "accuracy"}))
        is not None
    )
    root = tmp_path / ".athena" / "exp_docs"
    run_path = root / "runs" / "exp_search-1.json"
    before_run = run_path.read_bytes()
    before_alias = (root / "search.json").read_bytes()
    before_manifest = (root / "latest.json").read_bytes()
    payload = json.loads(run_path.read_text())
    payload["metric"]["primary"] = 999.0
    run_path.write_text(json.dumps(payload) + "\n")
    conflicting_run = run_path.read_bytes()

    outcome = projector.rebuild(
        context(completed_tree, task={"primary_metric": "accuracy"})
    )

    assert outcome is False
    assert run_path.read_bytes() == conflicting_run
    assert (root / "search.json").read_bytes() == before_alias
    assert (root / "latest.json").read_bytes() == before_manifest
    assert before_run != conflicting_run


def test_rebuild_does_not_invent_phase_failure_records(
    tmp_path: Path, completed_tree: ResearchTree
) -> None:
    outcome = projector_for(tmp_path).rebuild(context(completed_tree))
    assert outcome is True
    assert not list(
        (tmp_path / ".athena" / "exp_docs" / "runs").glob("*-phase-failure-*.json")
    )


def test_rebuild_keeps_baseline_alias_at_canonical_baseline_id(
    tmp_path: Path, completed_tree: ResearchTree
) -> None:
    completed_tree.add_hypothesis(
        Hypothesis(
            id="hyp-other-baseline",
            statement="other baseline statement",
            intervention="other baseline intervention",
            expected_effect="other baseline effect",
        )
    )
    completed_tree.add_experiment(
        "exp_other_baseline",
        Experiment(
            hypothesis_id="hyp-other-baseline",
            commit="other-baseline-commit",
            plan=ExperimentPlan(
                kind="baseline",
                change="other baseline change",
                run_config_ref="other-baseline-config",
                budget={},
                acceptance_rule="score",
            ),
            gitwork=GitWorkBranch(
                path="other-baseline-worktree",
                branch="other-baseline-branch",
                base_commit="other-baseline-base",
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_other_baseline",
                primary=0.99,
                per_sample="other-baseline-evidence",
            ),
        ),
    )

    outcome = projector_for(tmp_path).rebuild(
        context(completed_tree, task={"primary_metric": "accuracy"})
    )

    assert outcome is True
    root = tmp_path / ".athena" / "exp_docs"
    assert json.loads((root / "baseline.json").read_text())["run_id"] == "exp_baseline"


def test_empty_rebuild_without_existing_root_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    outcome = ExperimentDocumentProjector(root, ()).rebuild(context(ResearchTree()))
    assert outcome is True
    assert not root.exists()


def test_empty_rebuild_refreshes_existing_root_without_deleting_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "docs"
    unknown = root / "runs" / "unknown.json"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"opaque\n")

    outcome = ExperimentDocumentProjector(root, ()).rebuild(context(ResearchTree()))

    assert outcome is True
    assert unknown.read_bytes() == b"opaque\n"
    assert (root / "FINAL_REPORT.md").is_file()
    assert (root / "OPTIMIZATION.md").is_file()
    assert json.loads((root / "latest.json").read_text())["kind"] == "rebuild"

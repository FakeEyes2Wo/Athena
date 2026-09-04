"""Tests for durable experiment documents and research reports."""

from pathlib import Path

from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.exp_docs import (
    build_final_report,
    build_optimization_report,
    task_metric_name,
    write_stage_doc,
    write_reports,
)


def _tree(tmp_path: Path) -> ResearchTree:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="baseline",
            statement="Use a transfer baseline",
            intervention="fine-tune a pretrained backbone",
            expected_effect="improve macro-F1",
        )
    )
    tree.add_experiment(
        "exp_baseline",
        Experiment(
            hypothesis_id="baseline",
            commit="a" * 40,
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref="eval-ref",
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path=str(tmp_path), branch="main", base_commit="a" * 40
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline",
                primary=0.62,
                per_sample="evidence-ref",
            ),
            artifacts={"predictions": "pred-ref"},
        ),
    )
    tree.set_sota("exp_baseline")
    return tree


def test_write_stage_doc_updates_stage_and_latest_atomically(tmp_path: Path) -> None:
    path = write_stage_doc(
        tmp_path,
        {
            "run_id": "exp_h1",
            "stage": "search",
            "status": "SUCCEEDED",
            "metric": {
                "name": "macro-F1",
                "direction": "maximize",
                "primary": 0.71,
            },
            "artifacts": {"predictions": "sha256:pred"},
            "reason": {"kind": "trusted_score", "summary": "scored"},
            "provenance": {"dataset": "JWSSD_MW5"},
            "updated_at": "2026-09-03T01:02:03+00:00",
        },
    )

    assert path == tmp_path / ".athena" / "exp_docs" / "runs" / "exp_h1.json"
    payload = path.read_text(encoding="utf-8")
    latest = (path.parent.parent / "latest.json").read_text(encoding="utf-8")
    assert payload == latest
    assert payload == (path.parent.parent / "search.json").read_text(encoding="utf-8")
    assert '"schema_version": 1' in payload
    assert '"stage": "search"' in payload
    assert '"name": "macro-F1"' in payload
    assert '"direction": "maximize"' in payload
    assert '"updated_at": "2026-09-03T01:02:03+00:00"' in payload


def test_reports_include_tree_results_and_validation_guidance(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    validation = {
        "status": "COMPLETED",
        "test_score": 0.62,
        "final_test_score": 0.51,
        "generalization_gap": 0.11,
        "generalization_warning": True,
    }

    final = build_final_report(tree, validation)
    optimization = build_optimization_report(tree, validation, metric_name="macro-F1")

    assert "exp_baseline" in final
    assert "0.5100" in final
    assert "## 结果归因" in final
    assert "selected as SOTA" in final
    assert "generalization gap" in optimization.lower()
    assert "overfitting" in optimization.lower()
    assert "macro-F1" in optimization
    assert "Direction-aware improvement" in optimization


def test_write_reports_discloses_skipped_validation_without_final_metrics(
    tmp_path: Path,
) -> None:
    tree = _tree(tmp_path)

    final_path, optimization_path = write_reports(
        tmp_path, tree, None, validation_skipped=True
    )

    final = final_path.read_text(encoding="utf-8")
    optimization = optimization_path.read_text(encoding="utf-8")
    for document in (final, optimization):
        assert "VALIDATE 已跳过" in document
        assert "仅使用 SEARCH 结果" in document
        assert "final_test_score" not in document
        assert "generalization_gap" not in document
        assert "generalization gap" not in document.lower()


def test_metric_name_accepts_task_metadata_or_frozen_evaluator(tmp_path: Path) -> None:
    assert task_metric_name(tmp_path, {"primary_metric": "macro-F1"}) == "macro-F1"
    assert task_metric_name(tmp_path, {"primary_metric": {"name": "RMSE"}}) == "RMSE"
    metric = tmp_path / "workspaces" / "evaluator" / "evaluate" / "metric.json"
    metric.parent.mkdir(parents=True)
    metric.write_text('{"primary_metric":"balanced_accuracy"}', encoding="utf-8")
    assert task_metric_name(tmp_path, {"primary_metric": None}) == "balanced_accuracy"

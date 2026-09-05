"""Tests for deterministic experiment-document rendering."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.experiment_documents.models import (
    MetricRecord,
    ProvenanceRecord,
    ReasonRecord,
    StageRecord,
)
from athena.research.experiment_documents.store import (
    _build_manifest,
    _json_bytes,
    _render_stage_record,
)
from athena.research.report import build_final_report, build_optimization_report


def build_latest_manifest(*, kind, stage, run_id, files):
    return _build_manifest(
        SimpleNamespace(kind=kind, stage=stage, run_id=run_id), files
    )


@pytest.fixture
def stage_record() -> StageRecord:
    return StageRecord(
        run_id="exp_1",
        stage="search",
        status="SUCCEEDED",
        metric=MetricRecord(name="score", direction="maximize", primary=0.8),
        artifacts={"predictions": "artifact://predictions"},
        reason=ReasonRecord(kind="trusted_score", summary="scored"),
        provenance=ProvenanceRecord(phase="SEARCH", experiment_id="exp_1"),
    )


def test_stage_json_is_sorted_utf8_lf_and_final_newline(
    stage_record: StageRecord,
) -> None:
    rendered = _render_stage_record(stage_record)
    assert rendered.endswith(b"\n")
    assert b"\r\n" not in rendered
    assert json.loads(rendered) == stage_record.model_dump(mode="json")
    assert rendered.index(b'"artifacts"') < rendered.index(b'"metric"')


def test_manifest_hashes_exact_effective_bytes_and_is_content_derived() -> None:
    files = {"runs/exp_1.json": b"old bytes\n", "search.json": b"new bytes\n"}
    first = build_latest_manifest(
        kind="stage", stage="search", run_id="exp_1", files=files
    )
    second = build_latest_manifest(
        kind="stage",
        stage="search",
        run_id="exp_1",
        files=dict(reversed(files.items())),
    )
    assert first == second
    assert (
        first.files["runs/exp_1.json"]
        == hashlib.sha256(files["runs/exp_1.json"]).hexdigest()
    )
    assert (
        json.loads(_json_bytes(first.model_dump(mode="json")))["projection_id"]
        == first.projection_id
    )


def test_rebuild_manifest_has_no_stage_identity_and_is_path_order_independent() -> None:
    files = {"search.json": b"search", "FINAL_REPORT.md": b"report"}
    first = build_latest_manifest(kind="rebuild", stage=None, run_id=None, files=files)
    second = build_latest_manifest(
        kind="rebuild", stage=None, run_id=None, files=dict(reversed(files.items()))
    )
    assert first.stage is None
    assert first.run_id is None
    assert first.projection_id == second.projection_id


def _tree() -> ResearchTree:
    tree = ResearchTree()
    for hypothesis_id, statement in (
        ("baseline", "baseline"),
        ("high", "high"),
        ("low", "low"),
        ("failed", "failed"),
    ):
        tree.add_hypothesis(
            Hypothesis(
                id=hypothesis_id,
                statement=statement,
                intervention=f"change {statement}",
                expected_effect=f"score {statement}",
            )
        )

    def add_success(experiment_id: str, hypothesis_id: str, score: float) -> None:
        tree.add_experiment(
            experiment_id,
            Experiment(
                hypothesis_id=hypothesis_id,
                commit="a" * 40,
                plan=ExperimentPlan(
                    kind="search" if hypothesis_id != "baseline" else "baseline",
                    change="change",
                    run_config_ref="artifact://config",
                    budget={},
                    acceptance_rule="score",
                ),
                gitwork=GitWorkBranch(
                    path=f"C:/worktrees/{experiment_id}",
                    branch=experiment_id,
                    base_commit="a" * 40,
                ),
                status=ExperimentStatus.SUCCEEDED,
                eval=EvalResult(
                    experiment_id=experiment_id,
                    primary=score,
                    per_sample="artifact://evidence",
                ),
            ),
        )

    add_success("exp_baseline", "baseline", 0.5)
    add_success("exp_low", "low", 0.7)
    add_success("exp_high", "high", 0.9)
    tree.add_experiment(
        "exp_failed",
        Experiment(
            hypothesis_id="failed",
            commit="b" * 40,
            plan=ExperimentPlan(
                kind="search",
                change="change",
                run_config_ref="artifact://config",
                budget={},
                acceptance_rule="score",
            ),
            gitwork=GitWorkBranch(
                path="C:/worktrees/exp_failed",
                branch="exp_failed",
                base_commit="a" * 40,
            ),
            status=ExperimentStatus.FAILED,
            error="scoring failed",
        ),
    )
    tree.set_sota("exp_high")
    return tree


def test_disk_final_report_is_exact_shared_builder_bytes() -> None:
    tree = _tree()
    validation = {"final_test_score": 0.88, "generalization_gap": 0.02}
    rendered = build_final_report(tree, validation, validation_skipped=False).encode(
        "utf-8"
    )
    assert rendered.decode("utf-8") == build_final_report(
        tree, validation, validation_skipped=False
    )


def test_optimization_report_sorts_for_both_directions() -> None:
    tree = _tree()
    maximize = build_optimization_report(
        tree, None, metric_name="score", direction="maximize", validation_skipped=False
    )
    minimize = build_optimization_report(
        tree, None, metric_name="score", direction="minimize", validation_skipped=False
    )
    assert maximize.index("`exp_high`") < maximize.index("`exp_low`")
    assert minimize.index("`exp_low`") < minimize.index("`exp_high`")


def test_optimization_report_escapes_dynamic_markdown_cells() -> None:
    rendered = build_optimization_report(
        _tree(),
        None,
        metric_name="score|unsafe\nline",
        direction="maximize",
        validation_skipped=False,
    )
    assert "score\\|unsafe line" in rendered


def test_optimization_report_retains_unscored_sota_format_and_validation() -> None:
    tree = _tree()
    rendered = build_optimization_report(
        tree,
        {"final_test_score": 0.88, "test_score": 0.9, "generalization_gap": 0.02},
        metric_name="score",
        direction="maximize",
        validation_skipped=False,
    )
    assert "| 1 | `exp_high` | 0.900000 |" in rendered
    assert "| 是 |" in rendered
    assert "## 未产出分数" in rendered
    assert "exp_failed" in rendered
    assert "## VALIDATE" in rendered
    assert "final_test_score：0.88" in rendered
    assert rendered.endswith("\n")


def test_optimization_report_retains_legacy_summary_and_guidance() -> None:
    rendered = build_optimization_report(
        _tree(),
        {"final_test_score": 0.88, "test_score": 0.9, "generalization_gap": 0.02},
        metric_name="score",
        direction="maximize",
        validation_skipped=False,
    )
    assert "Successful experiments: 3" in rendered
    assert "Failed experiments: 1" in rendered
    assert "Observed signal" in rendered
    assert "Direction-aware improvement: `+0.400000`" in rendered
    assert "Freeze the winning commit" in rendered
    assert "Generalization gap: `0.0200`" in rendered
    assert "overfitting checks" in rendered
    assert "Failure-driven actions" in rendered
    assert "validate prediction ids" in rendered


def test_optimization_report_retains_empty_fallback() -> None:
    rendered = build_optimization_report(
        ResearchTree(),
        None,
        metric_name="score",
        direction="maximize",
        validation_skipped=False,
    )
    assert "No completed experiment is available for optimization yet." in rendered


def test_optimization_report_discloses_skipped_validation() -> None:
    rendered = build_optimization_report(
        _tree(),
        None,
        metric_name="score",
        direction="maximize",
        validation_skipped=True,
    )
    assert "VALIDATE 已跳过" in rendered

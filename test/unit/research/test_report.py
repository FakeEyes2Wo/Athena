"""Tests for the deterministic final research report builder."""

from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.report import build_final_report

_EVIDENCE = "sha256:" + "e" * 64
_RUN_CONFIG = "artifact://sha256:" + "c" * 64


def _tree_with_sota() -> ResearchTree:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="baseline",
            statement="baseline claim",
            intervention="fit baseline",
            expected_effect="establish reference",
        )
    )
    tree.add_experiment(
        "exp_baseline",
        Experiment(
            hypothesis_id="baseline",
            commit="c0",
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref=_RUN_CONFIG,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path="C:/worktrees/baseline", branch="main", base_commit="c0"
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline", primary=0.8, per_sample=_EVIDENCE
            ),
        ),
    )
    tree.set_sota("exp_baseline")
    tree.add_hypothesis(
        Hypothesis(
            id="h1",
            parent_id="exp_baseline",
            statement="pending claim",
            intervention="change the model",
            expected_effect="improve score",
        )
    )
    return tree


def test_report_surfaces_sota_and_validation_metrics() -> None:
    report = build_final_report(
        _tree_with_sota(),
        {
            "final_test_score": 0.79,
            "generalization_gap": 0.01,
            "generalization_warning": True,
        },
    )

    assert report.startswith("# Athena 研究报告")
    assert "## SOTA" in report
    assert "`exp_baseline`" in report
    assert "0.8000" in report
    assert "## 验证结果" in report
    assert "0.7900" in report
    assert "泛化警告" in report


def test_report_omits_validation_when_absent() -> None:
    report = build_final_report(_tree_with_sota(), None)

    assert "## 验证结果" not in report


def test_report_pending_section_excludes_executed_baseline() -> None:
    report = build_final_report(_tree_with_sota(), None)

    pending = report.split("## 待选假设", 1)[1].split("## 实验记录", 1)[0]
    assert "pending claim" in pending
    assert "baseline claim" not in pending


def test_report_lists_experiment_records() -> None:
    report = build_final_report(_tree_with_sota(), None)

    assert "## 实验记录" in report
    assert "[SUCCEEDED]" in report
    assert "baseline claim" in report

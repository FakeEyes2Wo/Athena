"""Tests for the deterministic final research report builder."""

import pytest

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
    assert "0.7900" in report
    assert "## 验证结果" in report
    assert "泛化警告" in report


def test_report_discloses_skipped_validate_without_final_metrics() -> None:
    report = build_final_report(_tree_with_sota(), None, validation_skipped=True)

    assert "VALIDATE 已跳过" in report
    assert "仅使用 SEARCH 结果" in report
    assert "final_test_score" not in report
    assert "generalization_gap" not in report


def test_report_rejects_skip_marker_with_real_validation() -> None:
    with pytest.raises(ValueError, match="cannot be both skipped and present"):
        build_final_report(
            _tree_with_sota(),
            {"final_test_score": 0.79},
            validation_skipped=True,
        )


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


def _tree_with_two_experiments() -> ResearchTree:
    """SOTA plus one weaker scored candidate and one failed run."""
    tree = _tree_with_sota()
    tree.add_experiment(
        "exp_h1",
        Experiment(
            hypothesis_id="h1",
            parent_id="exp_baseline",
            commit="c1",
            plan=ExperimentPlan(
                kind="search",
                change="swap the model",
                run_config_ref=_RUN_CONFIG,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path="C:/worktrees/h1", branch="athena/h1", base_commit="c0"
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_h1",
                primary=0.75,
                secondary={"TSS": 0.94, "HSS": 0.68},
                per_sample=_EVIDENCE,
            ),
        ),
    )
    tree.add_hypothesis(
        Hypothesis(
            id="h2",
            parent_id="exp_baseline",
            statement="third claim",
            intervention="add context windows",
            expected_effect="fewer false alarms",
        )
    )
    tree.add_experiment(
        "exp_h2",
        Experiment(
            hypothesis_id="h2",
            parent_id="exp_baseline",
            commit="c2",
            plan=ExperimentPlan(
                kind="candidate",
                change="add context",
                run_config_ref=_RUN_CONFIG,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path="C:/worktrees/h2", branch="athena/h2", base_commit="c0"
            ),
            status=ExperimentStatus.FAILED,
            error="command failed (exit 1): ModuleNotFoundError: astropy",
        ),
    )
    return tree


def test_report_compares_each_experiment_against_the_sota() -> None:
    """只报一个最终分数看不出任何一步改动是否真的有用。"""
    report = build_final_report(_tree_with_two_experiments(), None)

    table = report.split("## 迭代对照", 1)[1].split("##", 1)[0]
    assert "| `exp_h1` |" in table
    assert "0.7500" in table
    assert "-0.0500" in table  # 相对 SOTA 的增减带符号
    assert "+0.0000" in table  # SOTA 自己那一行


def test_report_surfaces_failed_experiments_with_their_reason() -> None:
    """缺了这一节，"跑 3 次成 2 次"和"跑 2 次全成"在材料里长得一模一样。"""
    report = build_final_report(_tree_with_two_experiments(), None)

    assert "## 失败实验与原因" in report
    assert "exp_h2" in report
    assert "ModuleNotFoundError: astropy" in report


def test_report_renders_secondary_metrics_of_the_sota() -> None:
    tree = _tree_with_two_experiments()
    tree.set_sota("exp_h1")

    report = build_final_report(tree, None)

    assert "次要指标" in report
    assert "TSS 0.9400" in report
    assert "HSS 0.6800" in report


def test_next_step_plan_names_the_intervention_and_its_expected_observation() -> None:
    """评审要的是"下一步做什么实验、看到什么才算成立"，不是一句 claim。"""
    report = build_final_report(_tree_with_sota(), None)

    plan = report.split("## 下一步验证方案", 1)[1].split("## 实验记录", 1)[0]
    assert "change the model" in plan
    assert "improve score" in plan

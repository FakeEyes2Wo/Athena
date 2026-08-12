"""Tests for retained validation calculations and result projection."""

import pytest

from athena.research.validation import (
    ValidationService,
    dependency_closure,
    generalization_gap,
    generalization_warning,
)

_GRAPH = {
    "feature-a": ["model-b"],
    "model-b": ["calibration-c"],
    "feature-x": ["model-b"],
}


def test_dependency_closure_removes_transitive_dependents() -> None:
    assert dependency_closure(_GRAPH, "feature-a") == (
        "feature-a",
        "model-b",
        "calibration-c",
    )


def test_dependency_closure_isolated_intervention_is_itself() -> None:
    assert dependency_closure(_GRAPH, "feature-x") == (
        "feature-x",
        "model-b",
        "calibration-c",
    )


def test_ablation_scope_covers_all_accepted_interventions() -> None:
    scope = ValidationService().ablation_scope(_GRAPH, ["feature-a", "feature-x"])
    assert scope["feature-a"] == ("feature-a", "model-b", "calibration-c")
    assert scope["feature-x"] == ("feature-x", "model-b", "calibration-c")


def test_generalization_gap_maximize_is_test_minus_final() -> None:
    assert generalization_gap(0.80, 0.75, "maximize") == pytest.approx(0.05)
    assert generalization_gap(0.80, 0.85, "maximize") == pytest.approx(-0.05)


def test_generalization_gap_minimize_is_final_minus_test() -> None:
    assert generalization_gap(0.10, 0.12, "minimize") == pytest.approx(0.02)
    assert generalization_gap(0.12, 0.10, "minimize") == pytest.approx(-0.02)


def test_generalization_warning_ignores_numeric_noise() -> None:
    assert generalization_warning(1e-13) is False
    assert generalization_warning(0.05) is True


def test_build_result_warns_but_still_completes() -> None:
    result = ValidationService().build_result(
        test_score=0.80,
        final_test_score=0.70,
        direction="maximize",
    )
    assert result.status == "COMPLETED"
    assert result.generalization_gap == pytest.approx(0.10)
    assert result.generalization_warning is True


def test_build_result_without_regression_has_no_warning() -> None:
    result = ValidationService().build_result(
        test_score=0.80,
        final_test_score=0.82,
        direction="maximize",
    )
    assert result.generalization_gap == pytest.approx(-0.02)
    assert result.generalization_warning is False

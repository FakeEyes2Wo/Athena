"""Statistical settlement tests."""

import pytest

from athena.research.supervisor.statistics import MetricEvidence, settle_statistically


def test_missing_uncertainty_is_inconclusive() -> None:
    assert (
        settle_statistically(
            MetricEvidence(metric=0.9),
            reference_metric=0.8,
            direction="maximize",
        )
        == "INCONCLUSIVE"
    )


def test_narrow_improvement_within_ci_is_inconclusive() -> None:
    assert (
        settle_statistically(
            MetricEvidence(metric=0.81, std_error=0.02, n=100),
            reference_metric=0.8,
            direction="maximize",
            min_effect_size=0.01,
        )
        == "INCONCLUSIVE"
    )


def test_robust_improvement_is_supported() -> None:
    assert (
        settle_statistically(
            MetricEvidence(metric=0.9, std_error=0.01, n=100),
            reference_metric=0.8,
            direction="maximize",
            min_effect_size=0.05,
        )
        == "SUPPORTED"
    )


def test_robust_regression_is_refuted() -> None:
    assert (
        settle_statistically(
            MetricEvidence(metric=0.7, std_error=0.01, n=100),
            reference_metric=0.8,
            direction="maximize",
            min_effect_size=0.05,
        )
        == "REFUTED"
    )


def test_family_size_makes_decision_more_conservative() -> None:
    evidence = MetricEvidence(metric=0.82, std_error=0.01, n=100)
    assert (
        settle_statistically(
            evidence,
            reference_metric=0.8,
            direction="maximize",
            min_effect_size=0.0,
            family_size=1,
        )
        == "SUPPORTED"
    )
    assert (
        settle_statistically(
            evidence,
            reference_metric=0.8,
            direction="maximize",
            min_effect_size=0.0,
            family_size=20,
        )
        == "INCONCLUSIVE"
    )


@pytest.mark.parametrize(
    ("alpha", "family_size"),
    [(0.0, 1), (1.1, 1), (0.05, 0)],
)
def test_invalid_statistical_parameters_raise(alpha: float, family_size: int) -> None:
    with pytest.raises(ValueError):
        settle_statistically(
            MetricEvidence(metric=0.9, std_error=0.01, n=100),
            reference_metric=0.8,
            direction="maximize",
            alpha=alpha,
            family_size=family_size,
        )

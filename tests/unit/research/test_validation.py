"""Tests for retained validation calculations and result projection."""

import pytest

from athena.research.evaluation import (
    generalization_gap,
    generalization_warning,
)


def test_generalization_gap_maximize_is_test_minus_final() -> None:
    assert generalization_gap(0.80, 0.75, "maximize") == pytest.approx(0.05)
    assert generalization_gap(0.80, 0.85, "maximize") == pytest.approx(-0.05)


def test_generalization_gap_minimize_is_final_minus_test() -> None:
    assert generalization_gap(0.10, 0.12, "minimize") == pytest.approx(0.02)
    assert generalization_gap(0.12, 0.10, "minimize") == pytest.approx(-0.02)


def test_generalization_warning_ignores_numeric_noise() -> None:
    assert generalization_warning(1e-13) is False
    assert generalization_warning(0.05) is True

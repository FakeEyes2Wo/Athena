"""Tests for Evaluator and Comparator."""

import pytest
from athena.core.schemas import MetricDef, EvalSpec, EvalResult
from athena.core.evaluation import Comparator


def test_comparator_detects_improvement():
    baseline = EvalResult(
        experiment_id="e1",
        primary=0.72,
        secondary={},
        per_sample="artifact://samples/e1",
    )
    candidate = EvalResult(
        experiment_id="e2",
        primary=0.78,
        secondary={},
        per_sample="artifact://samples/e2",
    )
    verdict = Comparator().compare(baseline, candidate)
    assert verdict.winner == "candidate"
    assert verdict.p_value < 0.05


def test_comparator_detects_tie():
    a = EvalResult(
        experiment_id="e1",
        primary=0.72,
        secondary={},
        per_sample="artifact://samples/e1",
    )
    b = EvalResult(
        experiment_id="e2",
        primary=0.7200005,
        secondary={},
        per_sample="artifact://samples/e2",
    )
    verdict = Comparator().compare(a, b)
    assert verdict.winner == "tie"
    assert verdict.p_value > 0.05

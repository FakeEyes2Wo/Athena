"""Evaluator 和 Comparator 的测试。"""

import math

import pytest
from pydantic import ValidationError
from athena.evaluation import Comparator
from athena.evaluation.comparator import compare_results
from athena.evaluation.evaluator import evaluate_predictions
from athena.evaluation.factory import create_eval_spec, default_eval_script
from athena.evaluation.types import ComparisonVerdict, EvalResult, EvalSpec, MetricDef


class SampleStore:
    def __init__(self) -> None:
        self._samples: dict[str, list[float]] = {}

    def put(self, samples: list[float]) -> str:
        ref = f"artifact://samples/{len(self._samples)}"
        self._samples[ref] = samples
        return ref

    def get(self, ref: str) -> list[float]:
        return self._samples[ref]


@pytest.fixture
def sample_store() -> SampleStore:
    return SampleStore()


def result(experiment_id: str, primary: float, per_sample: str) -> EvalResult:
    return EvalResult(
        experiment_id=experiment_id,
        primary=primary,
        per_sample=per_sample,
    )


def test_evaluation_types_validate_canonical_contracts() -> None:
    metric = MetricDef(
        name="rmse",
        direction="minimize",
        description="root mean square error",
    )
    spec = EvalSpec(primary=metric)
    evaluation = EvalResult(
        experiment_id="experiment-1",
        primary=0.42,
        per_sample="artifact://samples/experiment-1",
    )
    verdict = ComparisonVerdict(winner="candidate", p_value=0.01)

    assert spec.primary == metric
    assert evaluation.primary == 0.42
    assert verdict.winner == "candidate"

    with pytest.raises(ValidationError):
        MetricDef(
            name="rmse",
            direction="higher",
            description="root mean square error",
        )


def test_eval_spec_is_frozen() -> None:
    spec = EvalSpec(
        primary=MetricDef(
            name="rmse",
            direction="minimize",
            description="root mean square error",
        )
    )

    with pytest.raises(ValidationError):
        spec.test_ratio = 0.3


def test_eval_spec_freezes_evaluator_source() -> None:
    spec = create_eval_spec("classification")

    assert spec.eval_script
    assert spec.eval_script == default_eval_script(spec)
    assert "ATHENA_EXPERIMENT_ID" in spec.eval_script


def test_default_eval_script_supports_regression_metric() -> None:
    spec = create_eval_spec("regression")

    assert '"rmse"' in spec.eval_script
    assert "mean_squared_error" in spec.eval_script


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


def test_comparison_respects_minimize_direction(sample_store: SampleStore) -> None:
    baseline = result("base", 0.5, sample_store.put([0.6, 0.4]))
    candidate = result("cand", 0.3, sample_store.put([0.4, 0.2]))

    verdict = compare_results(
        baseline,
        candidate,
        direction="minimize",
        read_samples=sample_store.get,
    )

    assert verdict.winner == "candidate"
    assert verdict.p_value < 0.05


def test_equal_samples_are_a_tie(sample_store: SampleStore) -> None:
    ref = sample_store.put([1.0, 1.0, 1.0])

    verdict = compare_results(
        result("a", 1, ref),
        result("b", 1, ref),
        direction="maximize",
        read_samples=sample_store.get,
    )

    assert verdict.winner == "tie"
    assert verdict.p_value == 1.0


def test_comparison_rejects_mismatched_paired_samples(
    sample_store: SampleStore,
) -> None:
    baseline = result("base", 0.5, sample_store.put([0.6, 0.4]))
    candidate = result("cand", 0.3, sample_store.put([0.4]))

    with pytest.raises(ValueError, match="paired sample lengths differ"):
        compare_results(
            baseline,
            candidate,
            direction="minimize",
            read_samples=sample_store.get,
        )


def test_comparison_rejects_non_finite_samples(sample_store: SampleStore) -> None:
    baseline = result("base", 0.5, sample_store.put([0.6, math.nan]))
    candidate = result("cand", 0.3, sample_store.put([0.4, 0.2]))

    with pytest.raises(ValueError, match="paired samples must be finite"):
        compare_results(
            baseline,
            candidate,
            direction="minimize",
            read_samples=sample_store.get,
        )


def test_evaluate_predictions_persists_per_sample_squared_errors(
    sample_store: SampleStore,
) -> None:
    spec = EvalSpec(
        primary=MetricDef(
            name="rmse", direction="minimize", description="root mean square error"
        ),
        secondary=[
            MetricDef(name="mae", direction="minimize", description="mean error")
        ],
    )

    evaluation = evaluate_predictions(
        "exp_1", spec, [1.0, 2.0], [2.0, 2.0], sample_store.put
    )

    assert evaluation.experiment_id == "exp_1"
    assert evaluation.primary == pytest.approx(math.sqrt(0.5))
    assert evaluation.secondary == {"mae": 0.5}
    assert sample_store.get(evaluation.per_sample) == [1.0, 0.0]


def test_evaluate_predictions_calculates_each_metric_independently(
    sample_store: SampleStore,
) -> None:
    spec = EvalSpec(
        primary=MetricDef(
            name="mae", direction="minimize", description="mean absolute error"
        ),
        secondary=[
            MetricDef(
                name="rmse",
                direction="minimize",
                description="root mean square error",
            )
        ],
    )

    evaluation = evaluate_predictions(
        "exp_mixed", spec, [0.0, 4.0], [1.0, 0.0], sample_store.put
    )

    assert evaluation.primary == 2.5
    assert evaluation.secondary == {"rmse": pytest.approx(math.sqrt(8.5))}
    assert sample_store.get(evaluation.per_sample) == [1.0, 4.0]


def test_evaluate_predictions_executes_default_classification_spec(
    sample_store: SampleStore,
) -> None:
    evaluation = evaluate_predictions(
        "exp_2",
        create_eval_spec("classification"),
        [0.0, 1.0, 0.0],
        [0.0, 1.0, 1.0],
        sample_store.put,
    )

    assert evaluation.primary == pytest.approx(2 / 3)
    assert evaluation.secondary == pytest.approx(
        {"accuracy": 2 / 3, "precision_macro": 0.75, "recall_macro": 0.75}
    )
    assert sample_store.get(evaluation.per_sample) == [1.0, 1.0, 0.0]


def test_evaluate_predictions_executes_default_binary_spec(
    sample_store: SampleStore,
) -> None:
    evaluation = evaluate_predictions(
        "exp_3",
        create_eval_spec("binary_classification"),
        [0.0, 1.0, 0.0, 1.0],
        [0.1, 0.8, 0.6, 0.9],
        sample_store.put,
    )

    assert evaluation.primary == 1.0
    assert evaluation.secondary == pytest.approx(
        {"f1": 0.8, "precision": 2 / 3, "recall": 1.0}
    )
    assert sample_store.get(evaluation.per_sample) == [0.9, 0.8, 0.4, 0.9]


def test_create_eval_spec_uses_regression_defaults() -> None:
    spec = create_eval_spec("regression")

    assert spec.primary.name == "rmse"
    assert spec.primary.direction == "minimize"
    assert [metric.name for metric in spec.secondary] == ["mae", "r2"]


def test_comparator_delegates_to_paired_comparison(
    sample_store: SampleStore,
) -> None:
    baseline = result("base", 0.5, sample_store.put([0.6, 0.4]))
    candidate = result("cand", 0.3, sample_store.put([0.4, 0.2]))

    verdict = Comparator().compare(
        baseline,
        candidate,
        direction="minimize",
        read_samples=sample_store.get,
    )

    assert verdict.winner == "candidate"

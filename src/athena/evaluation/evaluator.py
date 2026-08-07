"""Evaluator: reads eval_result.json from CodeAgent output."""

import json
import math
from collections.abc import Callable, Sequence

from athena.core.contracts import ArtifactRef
from athena.evaluation.factory import metric_value
from athena.evaluation.types import EvalSpec, EvalResult, MetricDef


def evaluate_predictions(
    experiment_id: str,
    spec: EvalSpec,
    y_true: Sequence[float],
    y_pred: Sequence[float],
    write_samples: Callable[[Sequence[float]], ArtifactRef],
) -> EvalResult:
    """Evaluate one frozen specification and persist its paired samples."""
    primary, secondary, per_sample = metric_values(spec, y_true, y_pred)
    return EvalResult(
        experiment_id=experiment_id,
        primary=primary,
        secondary=secondary,
        per_sample=write_samples(per_sample),
    )


def metric_values(
    spec: EvalSpec, y_true: Sequence[float], y_pred: Sequence[float]
) -> tuple[float, dict[str, float], list[float]]:
    """Calculate frozen metrics and direction-compatible paired samples."""
    actual = [float(value) for value in y_true]
    predicted = [float(value) for value in y_pred]
    if len(actual) != len(predicted):
        raise ValueError("prediction lengths differ")
    if not actual:
        raise ValueError("predictions must not be empty")
    if not all(math.isfinite(value) for value in actual + predicted):
        raise ValueError("predictions must be finite")

    per_sample = _per_sample_values(spec.primary, actual, predicted)
    primary = metric_value(spec.primary.name, actual, predicted)
    secondary = {
        metric.name: metric_value(metric.name, actual, predicted)
        for metric in spec.secondary
    }
    return primary, secondary, per_sample


def _per_sample_values(
    metric: MetricDef, actual: list[float], predicted: list[float]
) -> list[float]:
    if metric.name in {"rmse", "mse", "r2"}:
        return [
            (truth - prediction) ** 2 for truth, prediction in zip(actual, predicted)
        ]
    if metric.name == "mae":
        return [abs(truth - prediction) for truth, prediction in zip(actual, predicted)]
    if metric.name == "roc_auc":
        positive = max(actual)
        return [
            prediction if truth == positive else 1.0 - prediction
            for truth, prediction in zip(actual, predicted)
        ]
    return [float(truth == prediction) for truth, prediction in zip(actual, predicted)]


class Evaluator:
    """Thin runner: reads eval_result.json produced by CodeAgent's eval.py."""

    def __init__(self, spec: EvalSpec):
        self._spec = spec

    def evaluate(self, predictions_ref: ArtifactRef) -> EvalResult:
        """Read eval_result.json from the artifact path and return EvalResult."""
        result_path = f"{predictions_ref.split(':')[1]}/eval_result.json"
        with open(result_path) as stream:
            data = json.load(stream)
        return EvalResult(
            experiment_id=data["experiment_id"],
            primary=data["primary"],
            secondary=data.get("secondary", {}),
            per_sample=f"{predictions_ref}/per_sample.csv",
        )

"""Evaluator: reads eval_result.json from CodeAgent output."""

import json
import math
from collections.abc import Callable, Sequence

from athena.core.contracts import ArtifactRef
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
    """Calculate the frozen metrics and direction-compatible paired samples."""
    actual = [float(value) for value in y_true]
    predicted = [float(value) for value in y_pred]
    if len(actual) != len(predicted):
        raise ValueError("prediction lengths differ")
    if not actual:
        raise ValueError("predictions must not be empty")
    if not all(math.isfinite(value) for value in actual + predicted):
        raise ValueError("predictions must be finite")

    per_sample = _per_sample_values(spec.primary, actual, predicted)
    primary = _metric_value(spec.primary, actual, predicted, per_sample)
    secondary = {
        metric.name: _metric_value(
            metric,
            actual,
            predicted,
            _per_sample_values(metric, actual, predicted),
        )
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


def _metric_value(
    metric: MetricDef,
    actual: list[float],
    predicted: list[float],
    per_sample: list[float],
) -> float:
    if metric.name == "rmse":
        return math.sqrt(sum(per_sample) / len(per_sample))
    if metric.name == "mse":
        return sum(per_sample) / len(per_sample)
    if metric.name == "mae":
        return sum(
            abs(truth - prediction) for truth, prediction in zip(actual, predicted)
        ) / len(actual)
    if metric.name == "r2":
        mean = sum(actual) / len(actual)
        total = sum((truth - mean) ** 2 for truth in actual)
        return 1.0 - sum(per_sample) / total if total else 0.0
    if metric.name == "accuracy":
        return sum(per_sample) / len(per_sample)
    if metric.name in {"f1_macro", "precision_macro", "recall_macro"}:
        return _macro_classification_metric(metric.name, actual, predicted)
    if metric.name in {"roc_auc", "f1", "precision", "recall"}:
        return _binary_classification_metric(metric.name, actual, predicted)
    raise ValueError(f"unsupported metric: {metric.name}")


def _macro_classification_metric(
    name: str, actual: list[float], predicted: list[float]
) -> float:
    values: list[float] = []
    for label in set(actual + predicted):
        true_positive = sum(
            truth == label and prediction == label
            for truth, prediction in zip(actual, predicted)
        )
        false_positive = sum(
            truth != label and prediction == label
            for truth, prediction in zip(actual, predicted)
        )
        false_negative = sum(
            truth == label and prediction != label
            for truth, prediction in zip(actual, predicted)
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        if name == "f1_macro":
            values.append(
                2 * precision * recall / (precision + recall)
                if precision + recall
                else 0.0
            )
        elif name == "precision_macro":
            values.append(precision)
        else:
            values.append(recall)
    return sum(values) / len(values)


def _binary_classification_metric(
    name: str, actual: list[float], predicted: list[float]
) -> float:
    labels = sorted(set(actual))
    if len(labels) != 2:
        raise ValueError("binary metrics require exactly two target values")
    negative, positive = labels
    if name == "roc_auc":
        positive_scores = [
            prediction
            for truth, prediction in zip(actual, predicted)
            if truth == positive
        ]
        negative_scores = [
            prediction
            for truth, prediction in zip(actual, predicted)
            if truth == negative
        ]
        return sum(
            (
                1.0
                if positive_score > negative_score
                else 0.5 if positive_score == negative_score else 0.0
            )
            for positive_score in positive_scores
            for negative_score in negative_scores
        ) / (len(positive_scores) * len(negative_scores))

    labels_predicted = [
        positive if prediction >= 0.5 else negative for prediction in predicted
    ]
    true_positive = sum(
        truth == positive and prediction == positive
        for truth, prediction in zip(actual, labels_predicted)
    )
    false_positive = sum(
        truth == negative and prediction == positive
        for truth, prediction in zip(actual, labels_predicted)
    )
    false_negative = sum(
        truth == positive and prediction == negative
        for truth, prediction in zip(actual, labels_predicted)
    )
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    if name == "precision":
        return precision
    if name == "recall":
        return recall
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


class Evaluator:
    """Thin runner: reads eval_result.json produced by CodeAgent's eval.py."""

    def __init__(self, spec: EvalSpec):
        self._spec = spec

    def evaluate(self, predictions_ref: ArtifactRef) -> EvalResult:
        """Read eval_result.json from the artifact path and return EvalResult."""
        result_path = f"{predictions_ref.split(':')[1]}/eval_result.json"
        with open(result_path) as f:
            data = json.load(f)
        return EvalResult(
            experiment_id=data["experiment_id"],
            primary=data["primary"],
            secondary=data.get("secondary", {}),
            per_sample=f"{predictions_ref}/per_sample.csv",
        )


if __name__ == "__main__":
    import tempfile
    import os as _os

    with tempfile.TemporaryDirectory() as tmp:
        result_data = {
            "experiment_id": "exp_001",
            "primary": 0.85,
            "secondary": {"accuracy": 0.83},
        }
        os.makedirs(_os.path.join(tmp, "artifact_ref"), exist_ok=True)
        with open(_os.path.join(tmp, "artifact_ref", "eval_result.json"), "w") as f:
            json.dump(result_data, f)
        spec = EvalSpec(
            primary={"name": "f1_macro", "direction": "maximize", "description": "F1"},
            secondary=[],
        )
        evaluator = Evaluator(spec)
        result = evaluator.evaluate(f"artifact://{tmp}/artifact_ref")
        print(f"EvalResult: {result}")

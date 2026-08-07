"""Host-owned evaluation of prediction files against private labels."""

import math
from pathlib import Path

import pandas as pd

from athena.evaluation.factory import metric_value
from athena.evaluation.types import EvaluationInputs, EvalResult, EvalSpec
from athena.storage import ArtifactStore

PREDICTION_COLUMN = "prediction"
PRIVATE_TARGET_COLUMN = "__athena_private_target"


class TrustedEvaluator:
    """Score submitted predictions without exposing held-out label values."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    async def evaluate(
        self,
        experiment_id: str,
        predictions_path: str | Path,
        inputs: EvaluationInputs,
        eval_spec: EvalSpec,
    ) -> EvalResult:
        """Validate, align, and score an experiment's prediction CSV."""
        predictions = pd.read_csv(predictions_path)
        labels = pd.read_csv(inputs.labels_path)
        row_id = inputs.row_id_column
        _validate_columns(predictions, [row_id, PREDICTION_COLUMN], "predictions")
        _validate_columns(labels, [row_id, inputs.target], "private labels")
        _validate_ids(predictions, row_id, "predictions")
        _validate_ids(labels, row_id, "private labels")

        if set(predictions[row_id]) != set(labels[row_id]):
            raise ValueError("prediction ID set must exactly match private labels")

        aligned = (
            labels.rename(columns={inputs.target: PRIVATE_TARGET_COLUMN})
            .merge(
                predictions,
                on=row_id,
                how="inner",
                validate="one_to_one",
            )
            .sort_values(row_id, kind="stable")
        )
        primary = _finite_metric(
            "primary",
            eval_spec.primary.name,
            aligned[PRIVATE_TARGET_COLUMN],
            aligned[PREDICTION_COLUMN],
        )
        secondary = {
            metric.name: _finite_metric(
                metric.name,
                metric.name,
                aligned[PRIVATE_TARGET_COLUMN],
                aligned[PREDICTION_COLUMN],
            )
            for metric in eval_spec.secondary
        }
        canonical = aligned[[row_id, PREDICTION_COLUMN]].to_csv(
            index=False,
            lineterminator="\n",
        )
        return EvalResult(
            experiment_id=experiment_id,
            primary=primary,
            secondary=secondary,
            per_sample=await self._artifacts.put_bytes(canonical.encode("utf-8")),
        )


def _validate_columns(frame: pd.DataFrame, expected: list[str], source: str) -> None:
    if list(frame.columns) != expected:
        raise ValueError(f"{source} columns must be exactly {expected}")


def _validate_ids(frame: pd.DataFrame, row_id: str, source: str) -> None:
    if frame[row_id].isna().any():
        raise ValueError(f"{source} contain null {row_id} values")
    if frame[row_id].duplicated().any():
        raise ValueError(f"{source} contain duplicate {row_id} values")


def _finite_metric(
    label: str, name: str, y_true: pd.Series, y_pred: pd.Series
) -> float:
    value = metric_value(name, y_true, y_pred)
    if not math.isfinite(value):
        raise ValueError(f"{label} metric must be finite")
    return value

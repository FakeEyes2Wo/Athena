import math
from pathlib import Path

import pandas as pd
import pytest

from athena.evaluation.trusted import TrustedEvaluator
from athena.evaluation.types import EvaluationInputs, EvalSpec, MetricDef
from athena.storage import LocalArtifactStore


def _inputs(tmp_path: Path, *, target: str = "label") -> EvaluationInputs:
    train_path = tmp_path / "train.csv"
    features_path = tmp_path / "features.csv"
    labels_path = tmp_path / "private-labels.csv"
    pd.DataFrame(
        {"__athena_row_id": [1, 2], "feature": [0.0, 1.0], target: [0, 1]}
    ).to_csv(train_path, index=False)
    pd.DataFrame({"__athena_row_id": [10, 20, 30], "feature": [0.0, 1.0, 2.0]}).to_csv(
        features_path, index=False
    )
    pd.DataFrame({"__athena_row_id": [10, 20, 30], target: [0, 1, 1]}).to_csv(
        labels_path, index=False
    )
    return EvaluationInputs(
        phase="validation",
        train_path=train_path,
        features_path=features_path,
        labels_path=labels_path,
        target=target,
    )


def _spec() -> EvalSpec:
    return EvalSpec(
        primary=MetricDef(
            name="accuracy", direction="maximize", description="Accuracy"
        ),
        secondary=[MetricDef(name="f1_macro", direction="maximize", description="F1")],
    )


def _write_predictions(path: Path, rows: list[tuple[object, object]]) -> Path:
    pd.DataFrame(rows, columns=["__athena_row_id", "prediction"]).to_csv(
        path, index=False
    )
    return path


@pytest.mark.asyncio
async def test_trusted_evaluator_scores_shuffled_predictions_with_private_labels(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    predictions_path = _write_predictions(
        tmp_path / "predictions.csv", [(30, 1), (10, 1), (20, 1)]
    )
    evaluator = TrustedEvaluator(LocalArtifactStore(tmp_path / "objects"))

    result = await evaluator.evaluate("exp-1", predictions_path, inputs, _spec())

    assert result.primary == pytest.approx(2 / 3)
    assert result.secondary["f1_macro"] == pytest.approx(0.4)
    assert result.per_sample.startswith("sha256:")
    canonical = pd.read_csv(
        (
            tmp_path
            / "objects"
            / result.per_sample.removeprefix("sha256:")[:2]
            / result.per_sample.removeprefix("sha256:")[2:]
        )
    )
    assert list(canonical.columns) == ["__athena_row_id", "prediction"]
    assert canonical["__athena_row_id"].tolist() == [10, 20, 30]


@pytest.mark.asyncio
async def test_trusted_evaluator_ignores_forged_adjacent_labels(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    predictions_path = _write_predictions(
        tmp_path / "predictions.csv", [(10, 1), (20, 1), (30, 1)]
    )
    pd.DataFrame({"label": [1, 1, 1]}).to_csv(tmp_path / "labels.csv", index=False)
    evaluator = TrustedEvaluator(LocalArtifactStore(tmp_path / "objects"))

    result = await evaluator.evaluate("exp-1", predictions_path, inputs, _spec())

    assert result.primary < 1.0
    assert result.per_sample.startswith("sha256:")


@pytest.mark.asyncio
async def test_trusted_evaluator_supports_target_named_prediction(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, target="prediction")
    predictions_path = _write_predictions(
        tmp_path / "predictions.csv", [(10, 0), (20, 1), (30, 1)]
    )
    evaluator = TrustedEvaluator(LocalArtifactStore(tmp_path / "objects"))

    result = await evaluator.evaluate("exp-1", predictions_path, inputs, _spec())

    assert result.primary == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "columns", "message"),
    [
        (
            [(10, 0), (10, 1), (30, 1)],
            ["__athena_row_id", "prediction"],
            "duplicate",
        ),
        ([(10, 0), (20, 1)], ["__athena_row_id", "prediction"], "ID set"),
        (
            [(10, 0), (20, 1), (30, 1), (40, 0)],
            ["__athena_row_id", "prediction"],
            "ID set",
        ),
        (
            [(10, 0), (None, 1), (30, 1)],
            ["__athena_row_id", "prediction"],
            "null",
        ),
        ([(10, 0), (20, 1), (30, 1)], ["__athena_row_id", "score"], "columns"),
    ],
)
async def test_trusted_evaluator_rejects_invalid_prediction_contract(
    tmp_path: Path,
    rows: list[tuple[object, object]],
    columns: list[str],
    message: str,
) -> None:
    inputs = _inputs(tmp_path)
    predictions_path = tmp_path / "predictions.csv"
    pd.DataFrame(rows, columns=columns).to_csv(predictions_path, index=False)
    evaluator = TrustedEvaluator(LocalArtifactStore(tmp_path / "objects"))

    with pytest.raises(ValueError, match=message):
        await evaluator.evaluate("exp-1", predictions_path, inputs, _spec())


@pytest.mark.asyncio
async def test_trusted_evaluator_rejects_non_finite_primary_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    predictions_path = _write_predictions(
        tmp_path / "predictions.csv", [(10, 0), (20, 1), (30, 1)]
    )
    monkeypatch.setattr("athena.evaluation.trusted.metric_value", lambda *_: math.nan)
    evaluator = TrustedEvaluator(LocalArtifactStore(tmp_path / "objects"))

    with pytest.raises(ValueError, match="primary metric must be finite"):
        await evaluator.evaluate("exp-1", predictions_path, inputs, _spec())

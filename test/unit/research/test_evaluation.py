"""TrustedEvaluator metric-validation tests (finite scalar primary)."""

import json
import math
from pathlib import Path

import pytest

from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import ScriptRunResult


class _FakeRunner:
    """返回固定 outputs 的 DataScriptRunner double。"""

    def __init__(self, outputs: dict[str, object]) -> None:
        self._outputs = outputs
        self.extra_files: dict[str, bytes] = {}

    async def run_dir(self, *_args, **_kwargs) -> ScriptRunResult:
        self.extra_files = _kwargs.get("extra_files", {})
        return ScriptRunResult(outputs=self._outputs)


def _evaluator(outputs: dict[str, object]) -> TrustedEvaluator:
    return TrustedEvaluator(_FakeRunner(outputs))


@pytest.mark.asyncio
async def test_score_rejects_non_finite_metric() -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", "Infinity"):
        with pytest.raises(ValueError, match="finite"):
            await _evaluator({"primary": bad}).score(
                evaluator_dir=Path("."),
                predictions={},
                candidate_id="c",
                direction="maximize",
                predictions_root="predictions",
            )


@pytest.mark.asyncio
async def test_score_rejects_non_scalar_metric() -> None:
    for bad in ([0.8], {"x": 1}, True):
        with pytest.raises(ValueError, match="number"):
            await _evaluator({"primary": bad}).score(
                evaluator_dir=Path("."),
                predictions={},
                candidate_id="c",
                direction="maximize",
                predictions_root="predictions",
            )


@pytest.mark.asyncio
async def test_score_accepts_finite_number_and_numeric_string() -> None:
    result = await _evaluator({"primary": 0.84}).score(
        evaluator_dir=Path("."),
        predictions={},
        candidate_id="c",
        direction="maximize",
        predictions_root="predictions",
    )
    assert result.test_score == 0.84
    assert math.isfinite(result.test_score)


@pytest.mark.asyncio
async def test_score_carries_optional_uncertainty_fields() -> None:
    result = await _evaluator({"primary": 0.84, "test_se": 0.02, "test_n": 100}).score(
        evaluator_dir=Path("."),
        predictions={},
        candidate_id="c",
        direction="maximize",
        predictions_root="predictions",
    )
    assert result.test_se == 0.02
    assert result.test_n == 100


@pytest.mark.asyncio
async def test_score_carries_public_metrics_artifact() -> None:
    result = await _evaluator(
        {"primary": 0.84, "metrics_ref": "sha256:" + "1" * 64}
    ).score(
        evaluator_dir=Path("."),
        predictions={},
        candidate_id="c",
        direction="maximize",
        predictions_root="predictions",
    )

    assert result.metrics_ref == "sha256:" + "1" * 64


@pytest.mark.asyncio
async def test_score_adapts_single_legacy_csv_to_declared_filename(tmp_path) -> None:
    (tmp_path / "metric.json").write_text(
        json.dumps(
            {
                "task_id": "future-task",
                "prediction_file": "predictions__future-task.csv",
                "prediction_id_column": "sample_id",
                "prediction_column": "pred_label",
                "probability_columns": [],
                "metrics_file": "metrics_public_test.csv",
                "eval_script": "eval_metrics.py",
            }
        ),
        encoding="utf-8",
    )
    runner = _FakeRunner({"primary": 0.84})

    await TrustedEvaluator(runner).score(
        evaluator_dir=tmp_path,
        predictions={"predictions.csv": b"sample_id,pred_label\na,0\n"},
        candidate_id="c",
        direction="maximize",
        predictions_root="predictions",
    )

    assert runner.extra_files["predictions/predictions__future-task.csv"] == (
        b"sample_id,pred_label\na,0\n"
    )


@pytest.mark.asyncio
async def test_score_rejects_invalid_uncertainty_fields() -> None:
    with pytest.raises(ValueError, match="test_se"):
        await _evaluator({"primary": 0.84, "test_se": -1}).score(
            evaluator_dir=Path("."),
            predictions={},
            candidate_id="c",
            direction="maximize",
            predictions_root="predictions",
        )
    with pytest.raises(ValueError, match="test_n"):
        await _evaluator({"primary": 0.84, "test_n": "many"}).score(
            evaluator_dir=Path("."),
            predictions={},
            candidate_id="c",
            direction="maximize",
            predictions_root="predictions",
        )

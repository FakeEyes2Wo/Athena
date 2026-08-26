"""TrustedEvaluator metric-validation tests (finite scalar primary)."""

import math

import pytest

from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import ScriptRunResult


class _FakeRunner:
    """返回固定 outputs 的 DataScriptRunner double。"""

    def __init__(self, outputs: dict[str, object]) -> None:
        self._outputs = outputs

    async def run(self, *_args, **_kwargs) -> ScriptRunResult:
        return ScriptRunResult(outputs=self._outputs)


def _evaluator(outputs: dict[str, object]) -> TrustedEvaluator:
    return TrustedEvaluator(_FakeRunner(outputs))


@pytest.mark.asyncio
async def test_score_rejects_non_finite_metric() -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", "Infinity"):
        with pytest.raises(ValueError, match="finite"):
            await _evaluator({"primary": bad}).score(
                eval_bundle=None,  # type: ignore[arg-type]
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
                eval_bundle=None,  # type: ignore[arg-type]
                predictions={},
                candidate_id="c",
                direction="maximize",
                predictions_root="predictions",
            )


@pytest.mark.asyncio
async def test_score_accepts_finite_number_and_numeric_string() -> None:
    result = await _evaluator({"primary": 0.84}).score(
        eval_bundle=None,  # type: ignore[arg-type]
        predictions={},
        candidate_id="c",
        direction="maximize",
        predictions_root="predictions",
    )
    assert result.test_score == 0.84
    assert math.isfinite(result.test_score)


@pytest.mark.asyncio
async def test_score_carries_optional_uncertainty_fields() -> None:
    result = await _evaluator(
        {"primary": 0.84, "test_se": 0.02, "test_n": 100}
    ).score(
        eval_bundle=None,  # type: ignore[arg-type]
        predictions={},
        candidate_id="c",
        direction="maximize",
        predictions_root="predictions",
    )
    assert result.test_se == 0.02
    assert result.test_n == 100


@pytest.mark.asyncio
async def test_score_rejects_invalid_uncertainty_fields() -> None:
    with pytest.raises(ValueError, match="test_se"):
        await _evaluator({"primary": 0.84, "test_se": -1}).score(
            eval_bundle=None,  # type: ignore[arg-type]
            predictions={},
            candidate_id="c",
            direction="maximize",
            predictions_root="predictions",
        )
    with pytest.raises(ValueError, match="test_n"):
        await _evaluator({"primary": 0.84, "test_n": "many"}).score(
            eval_bundle=None,  # type: ignore[arg-type]
            predictions={},
            candidate_id="c",
            direction="maximize",
            predictions_root="predictions",
        )

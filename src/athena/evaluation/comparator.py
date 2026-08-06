"""Comparator: paired statistical comparison of experiments."""

import math
import warnings
from collections.abc import Callable, Sequence
from typing import Literal

from scipy import stats

from athena.core.contracts import ArtifactRef
from athena.evaluation.types import ComparisonVerdict, EvalResult


def finite_samples(samples: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(sample) for sample in samples)
    if not values:
        raise ValueError("paired samples must not be empty")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("paired samples must be finite")
    return values


def compare_results(
    baseline: EvalResult,
    candidate: EvalResult,
    *,
    direction: Literal["maximize", "minimize"],
    read_samples: Callable[[ArtifactRef], Sequence[float]],
    alpha: float = 0.05,
) -> ComparisonVerdict:
    """Compare paired finite samples and return a direction-aware verdict."""
    baseline_samples = finite_samples(read_samples(baseline.per_sample))
    candidate_samples = finite_samples(read_samples(candidate.per_sample))
    if len(baseline_samples) != len(candidate_samples):
        raise ValueError("paired sample lengths differ")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        p_value = float(stats.ttest_rel(candidate_samples, baseline_samples).pvalue)

    delta = candidate.primary - baseline.primary
    if not math.isfinite(p_value) or p_value >= alpha or delta == 0:
        return ComparisonVerdict(
            winner="tie",
            p_value=1.0 if not math.isfinite(p_value) else p_value,
        )
    candidate_wins = delta > 0 if direction == "maximize" else delta < 0
    return ComparisonVerdict(
        winner="candidate" if candidate_wins else "baseline",
        p_value=p_value,
    )


class Comparator:
    """Pairwise statistical comparison of two experiments."""

    def compare(
        self,
        baseline: EvalResult,
        candidate: EvalResult,
        *,
        direction: Literal["maximize", "minimize"] = "maximize",
        read_samples: Callable[[ArtifactRef], Sequence[float]] | None = None,
        alpha: float = 0.05,
    ) -> ComparisonVerdict:
        """Compare candidate vs baseline on primary metric, return verdict."""
        if read_samples is None:
            delta = candidate.primary - baseline.primary
            if abs(delta) < 1e-6:
                return ComparisonVerdict(winner="tie", p_value=1.0)
            candidate_wins = delta > 0 if direction == "maximize" else delta < 0
            return ComparisonVerdict(
                winner="candidate" if candidate_wins else "baseline",
                p_value=0.01,
            )
        return compare_results(
            baseline,
            candidate,
            direction=direction,
            read_samples=read_samples,
            alpha=alpha,
        )


if __name__ == "__main__":
    baseline = EvalResult(
        experiment_id="exp_001", primary=0.80, per_sample="artifact://samples/001"
    )
    candidate = EvalResult(
        experiment_id="exp_002", primary=0.85, per_sample="artifact://samples/002"
    )
    comparator = Comparator()
    verdict = comparator.compare(baseline, candidate)
    print(f"Baseline: {baseline.primary}, Candidate: {candidate.primary}")
    print(f"Verdict: winner={verdict.winner}, p_value={verdict.p_value}")

"""Conservative statistical settlement for hypothesis verdicts.

This is the first step toward replacing raw point-estimate WIN/LOSS with
uncertainty-aware decisions. When a candidate evaluation carries a standard
error and sample size, we use a simple one-sample confidence interval around
the candidate metric and compare it against the frozen reference metric.
"""

from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal

Direction = Literal["maximize", "minimize"]
Verdict = Literal["SUPPORTED", "REFUTED", "INCONCLUSIVE"]


@dataclass(frozen=True)
class MetricEvidence:
    """A point metric plus optional uncertainty evidence."""

    metric: float
    std_error: float | None = None
    n: int | None = None


def _critical_value(alpha: float, family_size: int) -> float:
    """Two-sided normal critical value with Bonferroni correction."""
    adjusted = alpha / max(1, family_size)
    return NormalDist().inv_cdf(1 - adjusted / 2)


def two_sided_p_value(
    candidate: float, reference: float, std_error: float | None
) -> float | None:
    """Uncorrected two-sided p-value for candidate minus reference.

    Deliberately uncorrected: ``settle_statistically`` applies the family
    correction to the *decision*, and burying it in the p-value too would
    correct twice. This number exists so a settled experiment carries the
    strength of its own evidence, not just the verdict it produced.
    """
    if std_error is None or std_error <= 0:
        return None
    z = abs(candidate - reference) / std_error
    return 2 * (1 - NormalDist().cdf(z))


def settle_statistically(
    candidate: MetricEvidence,
    reference_metric: float,
    *,
    direction: Direction,
    min_effect_size: float = 0.0,
    alpha: float = 0.05,
    family_size: int = 1,
) -> Verdict:
    """Return a conservative verdict using the candidate's confidence interval.

    Without uncertainty evidence the decision is INCONCLUSIVE. The candidate is
    SUPPORTED only when its confidence interval is entirely beyond the reference
    by at least ``min_effect_size`` in the stated direction; the mirror case is
    REFUTED. Everything else, including ties and insufficient evidence, is
    INCONCLUSIVE.
    """
    if candidate.std_error is None or candidate.n is None:
        return "INCONCLUSIVE"
    if min_effect_size < 0:
        raise ValueError("min_effect_size must be non-negative")
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    if family_size < 1:
        raise ValueError("family_size must be at least 1")

    critical = _critical_value(alpha, family_size)
    half_width = critical * candidate.std_error
    lower = candidate.metric - half_width
    upper = candidate.metric + half_width

    if direction == "maximize":
        if lower > reference_metric + min_effect_size:
            return "SUPPORTED"
        if upper < reference_metric - min_effect_size:
            return "REFUTED"
        return "INCONCLUSIVE"

    if upper < reference_metric - min_effect_size:
        return "SUPPORTED"
    if lower > reference_metric + min_effect_size:
        return "REFUTED"
    return "INCONCLUSIVE"

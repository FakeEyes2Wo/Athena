"""Replaceable hypothesis scheduling policy boundary."""

import math
from enum import StrEnum
from typing import Protocol

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree


class Outcome(StrEnum):
    """Trusted result of a candidate against its frozen reference."""

    WIN = "WIN"
    DRAW = "DRAW"
    LOSS = "LOSS"


class HypothesisPolicy(Protocol):
    """Narrow policy used by the Supervisor scheduler."""

    def seed(self, parent: Hypothesis | None) -> float: ...

    def priority(self, hypothesis: Hypothesis, tree: ResearchTree) -> float: ...

    def settle(self, reference_priority: float, outcome: Outcome) -> float: ...


# TODO(search-policy): Replace EloPolicy with an evidence-aware scheduling
# policy after enough real-search traces exist. Keep Supervisor dependent only
# on HypothesisPolicy.
class EloPolicy:
    """Single-sided Elo update with a fixed 0.5 expected score."""

    ROOT_PRIORITY = 1000.0

    def __init__(self, *, k: float = 32.0) -> None:
        if not math.isfinite(k) or k <= 0:
            raise ValueError("Elo k must be a positive finite number")
        self._k = k

    def seed(self, parent: Hypothesis | None) -> float:
        return self.ROOT_PRIORITY if parent is None else parent.priority

    def priority(self, hypothesis: Hypothesis, tree: ResearchTree) -> float:
        del tree
        return hypothesis.priority

    def settle(self, reference_priority: float, outcome: Outcome) -> float:
        if not math.isfinite(reference_priority):
            raise ValueError("reference priority must be finite")
        score = {
            Outcome.WIN: 1.0,
            Outcome.DRAW: 0.5,
            Outcome.LOSS: 0.0,
        }[outcome]
        return reference_priority + self._k * (score - 0.5)


def queue_order(priority: float, order: int | None) -> tuple[float, int]:
    """Sort key for priority descending followed by FIFO order ascending."""
    if not math.isfinite(priority):
        raise ValueError("hypothesis priority must be finite")
    if order is None:
        raise ValueError("hypothesis order must be assigned before scheduling")
    return (-priority, order)

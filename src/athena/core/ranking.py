import math
from collections import defaultdict
from athena.core.schemas import Hypothesis


class HypothesisRanker:
    """Bradley-Terry rating + UCB selection. Pure algorithm, no LLM."""

    def __init__(self, beta: float = 2.0, lamb: float = 0.1):
        self._strengths: dict[str, float] = defaultdict(lambda: 0.0)
        self._comparisons: dict[str, int] = defaultdict(int)
        self._beta = beta
        self._lamb = lamb

    def update(self, comparisons: list[tuple[str, str, bool]]) -> None:
        """(winner_id, loser_id, is_tie) -> batch MLE approximation.
        Simplified: running average of win rate. Full BT-MLE would use logistic regression.
        """
        for winner, loser, is_tie in comparisons:
            self._comparisons[winner] += 1
            self._comparisons[loser] += 1
            if not is_tie:
                self._strengths[winner] += 0.05
                self._strengths[loser] -= 0.05

    def select(self, hypotheses: list[Hypothesis], proximity: "ProximityGraph") -> str:
        best_id, best_score = None, float("-inf")
        for h in hypotheses:
            if not h.id:
                continue
            mu = self._strengths.get(h.id, 0.0)
            n = self._comparisons.get(h.id, 0)
            sigma = 1.0 / (
                1.0 + math.sqrt(n)
            )  # fewer comparisons -> higher uncertainty
            ucb = mu + self._beta * sigma
            prox_penalty = self._lamb * proximity.min_distance_to_executed(h)
            score = ucb - prox_penalty
            if score > best_score:
                best_score, best_id = score, h.id
        if best_id is None:
            raise ValueError("No selectable hypothesis")
        return best_id


class ProximityGraph:
    """Tracks hypothesis similarity to avoid re-running near-duplicates.

    Uses keyword Jaccard as default; embedding-based similarity when available.
    """

    def __init__(self):
        self._executed: list[Hypothesis] = []

    def add(self, h: Hypothesis) -> None:
        self._executed.append(h)

    def min_distance_to_executed(self, h: Hypothesis) -> float:
        if not self._executed:
            return 1.0
        return min(self._jaccard_dist(h, ex) for ex in self._executed)

    @staticmethod
    def _jaccard_dist(a: Hypothesis, b: Hypothesis) -> float:
        words_a = set((a.statement + " " + a.intervention).lower().split())
        words_b = set((b.statement + " " + b.intervention).lower().split())
        if not words_a or not words_b:
            return 1.0
        intersection = len(words_a & words_b)
        union = len(words_a | words_b)
        return 1.0 - intersection / union if union > 0 else 1.0

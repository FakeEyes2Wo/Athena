"""Deterministic Bradley-Terry scoring, UCB selection, and proximity control."""

import math
import random
import statistics

from athena.core.research_models import Hypothesis

_BT_STEP = 0.1
_PROXIMITY_WEIGHT = 0.1
_BOOTSTRAP_SAMPLES = 64


def fit_bradley_terry(
    strengths: dict[str, float],
    comparisons: list[tuple[str, str]],
) -> dict[str, float]:
    """Fit centered item strengths with stable incremental BT updates."""
    ratings = dict(strengths)
    for winner, loser in comparisons:
        ratings.setdefault(winner, 0.0)
        ratings.setdefault(loser, 0.0)
        gap = max(-30.0, min(30.0, ratings[winner] - ratings[loser]))
        win_probability = 1.0 / (1.0 + math.exp(-gap))
        delta = _BT_STEP * (1.0 - win_probability)
        ratings[winner] += delta
        ratings[loser] -= delta
    if ratings:
        center = sum(ratings.values()) / len(ratings)
        ratings = {item_id: value - center for item_id, value in ratings.items()}
    return ratings


def selection_score(
    mean: float,
    uncertainty: float,
    min_distance: float,
    beta: float = 2.0,
) -> float:
    """Combine estimated quality, uncertainty, and duplicate penalty."""
    return mean + beta * uncertainty - _PROXIMITY_WEIGHT * (1.0 - min_distance)


class HypothesisRanker:
    """Rank hypotheses with deterministic BT estimates and UCB exploration."""

    def __init__(self, seed: int = 0):
        self._decisive: list[tuple[str, str]] = []
        self._seed = seed
        self._counts: dict[str, int] = {}
        self._strengths_cache: dict[str, float] | None = None

    def update(self, comparisons: list[tuple[str, str, bool]]) -> None:
        for winner, loser, is_tie in comparisons:
            if not is_tie:
                self._decisive.append((winner, loser))
                self._counts[winner] = self._counts.get(winner, 0) + 1
                self._counts[loser] = self._counts.get(loser, 0) + 1
        self._strengths_cache = None

    def _base_strengths(self) -> dict[str, float]:
        if self._strengths_cache is None:
            self._strengths_cache = fit_bradley_terry({}, self._decisive)
        return self._strengths_cache

    def estimate(self, hypothesis_id: str) -> tuple[float, float]:
        strengths = self._base_strengths()
        mean = strengths.get(hypothesis_id, 0.0)
        count = self._counts.get(hypothesis_id, 0)
        exploration = 1.0 / math.sqrt(count + 1.0)
        if not self._decisive:
            return mean, exploration
        rng = random.Random(f"{self._seed}:{hypothesis_id}:{len(self._decisive)}")
        samples = [
            fit_bradley_terry(
                {},
                [rng.choice(self._decisive) for _ in self._decisive],
            ).get(hypothesis_id, 0.0)
            for _ in range(_BOOTSTRAP_SAMPLES)
        ]
        return mean, max(exploration, statistics.pstdev(samples))

    def select(
        self,
        hypotheses: list[Hypothesis],
        proximity: "ProximityGraph",
        beta: float = 2.0,
    ) -> str:
        """Select the highest stable UCB score."""
        # TODO(advanced-ranking): Define and validate the ELO-style rubric that
        # selects the more advanced hypothesis; retain this stable policy until
        # pair construction, cold-start, and confidence rules are approved.
        scored: list[tuple[float, str]] = []
        for hypothesis in hypotheses:
            if not hypothesis.id:
                continue
            distance = proximity.min_distance_to_executed(hypothesis)
            score = selection_score(*self.estimate(hypothesis.id), distance, beta)
            scored.append((score, hypothesis.id))
        if not scored:
            raise ValueError("No selectable hypothesis")
        return max(scored)[1]


class ProximityGraph:
    """Track lexical proximity to hypotheses that have already run."""

    def __init__(self):
        self._executed: list[Hypothesis] = []
        self._word_cache: dict[str, set[str]] = {}

    def add(self, hypothesis: Hypothesis) -> None:
        self._executed.append(hypothesis)
        self._word_cache[hypothesis.id] = self._words(hypothesis)

    def min_distance_to_executed(self, hypothesis: Hypothesis) -> float:
        if not self._executed:
            return 1.0
        words = self._words(hypothesis)
        return min(self._cached_jaccard(words, item) for item in self._executed)

    def _cached_jaccard(self, words: set[str], executed: Hypothesis) -> float:
        executed_words = self._word_cache.get(executed.id, set())
        return self._distance(words, executed_words)

    @staticmethod
    def _words(hypothesis: Hypothesis) -> set[str]:
        return set(
            (hypothesis.statement + " " + hypothesis.intervention).lower().split()
        )

    @staticmethod
    def _distance(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 1.0
        union = len(left | right)
        return 1.0 - len(left & right) / union if union else 1.0

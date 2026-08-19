"""Hypothesis ranking and deduplication for rolling SEARCH.

Replaces the pure ``(-priority, order)`` FIFO sort with a small UCB-style
selector: a rubric cold-start prior + accumulated strength (a Bradley-Terry
proxy from the policy's real WIN/DRAW/LOSS rating) + a novelty exploration
bonus - a normalized cost penalty. All signals are deterministic and pure
functions of the tree, so they stay testable and reproducible.
"""

import math
import re

from pydantic import BaseModel, ConfigDict, Field

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    """Lowercase word tokens for a cheap, deterministic similarity."""
    return set(_WORD.findall(text.lower()))


def jaccard(left: set[str], right: set[str]) -> float:
    """Jaccard similarity; an empty union yields 0.0."""
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def _text_similarity(left: Hypothesis, right: Hypothesis) -> float:
    """Similarity over statement + intervention tokens (the dedup key)."""
    a = tokenize(left.statement) | tokenize(left.intervention)
    b = tokenize(right.statement) | tokenize(right.intervention)
    return jaccard(a, b)


def rubric_prior(hypothesis: Hypothesis, tree: ResearchTree) -> float:
    """Cold-start prior in [0.4, 1.0] from specificity alone.

    A pending hypothesis has no experiment data yet, so this rubric is the only
    reliable signal. It is deliberately small and transparent; a future
    ReflectionAgent rubric can replace it with richer scores.

    **有引用不再加分。** 早先这里给非空 ``sources`` 加 0.3（占总分 0.12），意图是让有据
    可依的假设先跑。真机（2026-08-16 第 12 次）表明它买到的是装饰而不是依据：带引用与
    不带引用的假设提的是同一批干预，引用是事后贴上去的，且经常张冠李戴——一篇《数据增强
    综述》被用来支持"两两交互特征"。奖励"有没有引用"就是在为贴标签付钱。

    引用本身仍然有价值（可追溯、可复核），只是不该换算成优先级。真要让文献影响排序，得
    先有"这条引用确实支持这个主张"的判据，而那还不存在。
    """
    del tree
    specific = 1.0 if len(tokenize(hypothesis.intervention)) >= 3 else 0.0
    return 0.4 + 0.6 * specific


def novelty(hypothesis: Hypothesis, tree: ResearchTree) -> float:
    """1 - max similarity to settled hypotheses (1.0 when nothing is settled).

    This is the UCB exploration term: it rewards directions unlike anything
    already tested, instead of repeating near-duplicate interventions.
    """
    settled = [item for item in tree.hypotheses() if item.status != "PROPOSED"]
    if not settled:
        return 1.0
    return 1.0 - max(_text_similarity(hypothesis, other) for other in settled)


def deduplicate(
    candidates: list[Hypothesis],
    existing: list[Hypothesis],
    threshold: float,
) -> list[Hypothesis]:
    """Keep candidates distinct from ``existing`` and from each other.

    Greedy in input order (earlier candidates win ties). ``existing`` should be
    every hypothesis already in the graph, regardless of status.
    """
    kept: list[Hypothesis] = []
    seen: list[Hypothesis] = list(existing)
    for candidate in candidates:
        if any(_text_similarity(candidate, other) >= threshold for other in seen):
            continue
        kept.append(candidate)
        seen.append(candidate)
    return kept


class RankConfig(BaseModel):
    """Weights for the selector's deterministic score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    strength_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    novelty_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    cost_weight: float = Field(default=0.1, ge=0.0)
    dedup_threshold: float = Field(default=0.8, gt=0.0, le=1.0)


class Selector:
    """Rank pending hypotheses by ``prior + strength + novelty - cost``.

    ``strength`` is the policy's accumulated rating — the Bradley-Terry proxy
    fed by real experiment WIN/DRAW/LOSS outcomes; ``novelty`` is the UCB
    exploration bonus; ``cost`` is a normalized penalty for expensive work.
    """

    def __init__(self, policy, config: RankConfig | None = None) -> None:
        self._policy = policy
        self._config = config or RankConfig()

    @property
    def dedup_threshold(self) -> float:
        """Threshold shared by ranking-time dedup callers."""
        return self._config.dedup_threshold

    def deduplicate(
        self, candidates: list[Hypothesis], existing: list[Hypothesis]
    ) -> list[Hypothesis]:
        """Drop candidates too similar to the graph or to each other."""
        return deduplicate(candidates, existing, self._config.dedup_threshold)

    def rank(
        self, tree: ResearchTree, candidates: list[Hypothesis]
    ) -> list[Hypothesis]:
        """Return candidates sorted best-first, tie-broken by ``order``."""
        strengths = self._normalized_strengths(candidates, tree)
        return sorted(
            candidates,
            key=lambda h: (-self._score(h, tree, strengths), h.order or 0),
        )

    def _normalized_strengths(
        self, candidates: list[Hypothesis], tree: ResearchTree
    ) -> dict[str | None, float]:
        priorities = {
            hypothesis.id: self._policy.priority(hypothesis, tree)
            for hypothesis in candidates
            if hypothesis.id is not None
        }
        values = [value for value in priorities.values() if math.isfinite(value)]
        low = min(values) if values else 0.0
        high = max(values) if values else 0.0
        span = high - low
        if span <= 0:
            return {key: 0.5 for key in priorities}
        return {key: (value - low) / span for key, value in priorities.items()}

    def _score(
        self,
        hypothesis: Hypothesis,
        tree: ResearchTree,
        strengths: dict[str | None, float],
    ) -> float:
        config = self._config
        prior = rubric_prior(hypothesis, tree)
        strength = strengths.get(hypothesis.id, 0.5)
        explore = novelty(hypothesis, tree)
        cost = min(max(hypothesis.cost, 0.0), 1.0)
        return (
            config.prior_weight * prior
            + config.strength_weight * strength
            + config.novelty_weight * explore
            - config.cost_weight * cost
        )

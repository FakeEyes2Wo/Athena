"""Policy, ranking, and deterministic slot scheduling for SEARCH."""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.research.supervisor.state import ResearchState


class Outcome(StrEnum):
    """Trusted result of a candidate against its frozen reference."""

    WIN = "WIN"
    DRAW = "DRAW"
    LOSS = "LOSS"


class HypothesisPolicy(Protocol):
    """Narrow policy used by SEARCH ranking and settlement."""

    def seed(self, parent: Hypothesis | None) -> float:
        """Return the initial priority for a new child hypothesis."""
        ...

    def priority(self, hypothesis: Hypothesis, tree: ResearchTree) -> float:
        """Return the scheduling priority for an existing hypothesis."""
        ...

    def settle(self, reference_priority: float, outcome: Outcome) -> float:
        """Return a candidate priority after its trusted comparison."""
        ...


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
        """Inherit a parent's priority or seed the root priority."""
        return self.ROOT_PRIORITY if parent is None else parent.priority

    def priority(self, hypothesis: Hypothesis, tree: ResearchTree) -> float:
        """Use the durable hypothesis priority without derived state."""
        del tree
        return hypothesis.priority

    def settle(self, reference_priority: float, outcome: Outcome) -> float:
        """Apply one fixed-expectation Elo update."""
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
    """Return a cold-start prior in [0.4, 1.0] from intervention specificity."""
    del tree
    specific = 1.0 if len(tokenize(hypothesis.intervention)) >= 3 else 0.0
    return 0.4 + 0.6 * specific


def novelty(hypothesis: Hypothesis, tree: ResearchTree) -> float:
    """Return one minus the greatest similarity to settled hypotheses."""
    settled = [item for item in tree.hypotheses() if item.status != "PROPOSED"]
    if not settled:
        return 1.0
    return 1.0 - max(_text_similarity(hypothesis, other) for other in settled)


def deduplicate(
    candidates: list[Hypothesis],
    existing: list[Hypothesis],
    threshold: float,
) -> list[Hypothesis]:
    """Keep candidates distinct from existing hypotheses and each other."""
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
    """Rank pending hypotheses by prior, strength, novelty, and cost."""

    def __init__(
        self, policy: HypothesisPolicy, config: RankConfig | None = None
    ) -> None:
        self._policy = policy
        self._config = config or RankConfig()

    @property
    def dedup_threshold(self) -> float:
        """Return the threshold shared by ranking-time dedup callers."""
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
            return dict.fromkeys(priorities, 0.5)
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


_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class ScheduleKind(StrEnum):
    """Kind of one scheduling action emitted to fill a SEARCH slot."""

    RESUME = "RESUME"
    START_NEW = "START_NEW"
    START_NEXT_HYPOTHESIS = "START_NEXT_HYPOTHESIS"
    GENERATE = "GENERATE"


@dataclass(frozen=True)
class ScheduleAction:
    """One deterministic slot-filling action."""

    kind: ScheduleKind
    plan_id: str | None = None
    hypothesis_id: str | None = None
    count: int = 0

    @classmethod
    def Resume(cls, plan_id: str) -> "ScheduleAction":
        """Resume an existing READY Plan into a free slot."""
        return cls(kind=ScheduleKind.RESUME, plan_id=plan_id)

    @classmethod
    def StartNew(cls, hypothesis_id: str) -> "ScheduleAction":
        """Create and start a Plan for one queued Hypothesis."""
        return cls(kind=ScheduleKind.START_NEW, hypothesis_id=hypothesis_id)

    @classmethod
    def StartNextHypothesis(cls, hypothesis_id: str) -> "ScheduleAction":
        """Create the single Human-requested next Hypothesis Plan."""
        return cls(kind=ScheduleKind.START_NEXT_HYPOTHESIS, hypothesis_id=hypothesis_id)

    @classmethod
    def Generate(cls, count: int) -> "ScheduleAction":
        """Ask for at least ``count`` new Hypotheses."""
        return cls(kind=ScheduleKind.GENERATE, count=count)


Resume = ScheduleAction.Resume
StartNew = ScheduleAction.StartNew
StartNextHypothesis = ScheduleAction.StartNextHypothesis
Generate = ScheduleAction.Generate


def count_search_attempts(state: ResearchState, tree: ResearchTree) -> int:
    """Return completed SEARCH experiments plus active SEARCH Plans."""
    settled = sum(
        1
        for experiment in tree.experiments(kind="search")
        if experiment.status in _TERMINAL
    )
    active = sum(1 for plan in state.plans.values() if plan.kind == "SEARCH")
    return settled + active


class Scheduler:
    """Fill SEARCH concurrency slots in a fixed deterministic order."""

    def __init__(
        self,
        policy: HypothesisPolicy | None = None,
        selector: Selector | None = None,
    ) -> None:
        self._policy = policy or EloPolicy()
        self._selector = selector or Selector(self._policy)

    def seed(self, parent: Hypothesis | None) -> float:
        """Seed one hypothesis through the scheduler's policy."""
        return self._policy.seed(parent)

    def settle(self, reference_priority: float, outcome: Outcome) -> float:
        """Settle one hypothesis through the scheduler's policy."""
        return self._policy.settle(reference_priority, outcome)

    def deduplicate(
        self, candidates: list[Hypothesis], existing: list[Hypothesis]
    ) -> list[Hypothesis]:
        """Drop candidates too similar to the graph before registration."""
        return self._selector.deduplicate(candidates, existing)

    def next_actions(
        self,
        state: ResearchState,
        tree: ResearchTree,
        running_ids: Iterable[str],
        *,
        human_next: str | None = None,
        manual: bool = False,
    ) -> list[ScheduleAction]:
        """Project slot-filling actions without mutating scheduling state."""
        if state.phase != "SEARCH":
            return []

        running = set(running_ids)
        free_slots = max(0, state.concurrency - len(running))
        actions: list[ScheduleAction] = []

        for plan_id, plan in state.plans.items():
            if free_slots == 0:
                break
            if (
                plan.kind == "SEARCH"
                and plan_id not in running
                and (plan.turn_limit is None or plan.turns_used < plan.turn_limit)
            ):
                actions.append(ScheduleAction.Resume(plan_id))
                free_slots -= 1

        create_budget = state.search_limit - count_search_attempts(state, tree)
        if create_budget > 0 and free_slots > 0:
            if human_next is not None:
                actions.append(ScheduleAction.StartNextHypothesis(human_next))
                free_slots -= 1
                create_budget -= 1
            if manual:
                if (
                    not tree.pending_hypotheses()
                    and free_slots > 0
                    and create_budget > 0
                ):
                    actions.append(
                        ScheduleAction.Generate(min(free_slots, create_budget))
                    )
            else:
                for hypothesis in self._queued(tree, state, human_next):
                    if free_slots == 0 or create_budget == 0:
                        break
                    actions.append(ScheduleAction.StartNew(hypothesis.id))
                    free_slots -= 1
                    create_budget -= 1
                if free_slots > 0 and create_budget > 0:
                    actions.append(
                        ScheduleAction.Generate(min(free_slots, create_budget))
                    )
        return actions

    def _queued(
        self, tree: ResearchTree, state: ResearchState, human_next: str | None
    ) -> list[Hypothesis]:
        executed = {experiment.hypothesis_id for experiment in tree.experiments()}
        candidates = [
            hypothesis
            for hypothesis in tree.pending_hypotheses()
            if hypothesis.id is not None
            and hypothesis.id not in state.plans
            and hypothesis.id != human_next
            and hypothesis.id not in executed
        ]
        return self._selector.rank(tree, self._selector.deduplicate(candidates, []))


__all__ = [
    "EloPolicy",
    "Generate",
    "HypothesisPolicy",
    "Outcome",
    "RankConfig",
    "Resume",
    "ScheduleAction",
    "ScheduleKind",
    "Scheduler",
    "Selector",
    "StartNew",
    "StartNextHypothesis",
    "count_search_attempts",
    "deduplicate",
    "jaccard",
    "novelty",
    "queue_order",
    "rubric_prior",
    "tokenize",
]

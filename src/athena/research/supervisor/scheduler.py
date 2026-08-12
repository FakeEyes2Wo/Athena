"""Pure deterministic scheduling for rolling SEARCH Plan slots."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.research.supervisor.policy import (
    EloPolicy,
    HypothesisPolicy,
    Outcome,
    queue_order,
)
from athena.research.supervisor.state import ResearchState

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class ScheduleKind(StrEnum):
    """Kind of one scheduling action emitted to fill a SEARCH slot."""

    RESUME = "RESUME"
    START_NEW = "START_NEW"
    START_NEXT_HYPOTHESIS = "START_NEXT_HYPOTHESIS"
    GENERATE = "GENERATE"


@dataclass(frozen=True)
class ScheduleAction:
    """One deterministic slot-filling action.

    ``Resume`` re-enables an existing READY Plan, ``StartNew`` creates a Plan
    from a queued Hypothesis, ``StartNextHypothesis`` creates the single
    Human-requested next Plan, and ``Generate`` asks for new Hypotheses.
    """

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
        """Ask the SupervisorAgent for exactly ``count`` new Hypotheses."""
        return cls(kind=ScheduleKind.GENERATE, count=count)


Resume = ScheduleAction.Resume
StartNew = ScheduleAction.StartNew
StartNextHypothesis = ScheduleAction.StartNextHypothesis
Generate = ScheduleAction.Generate


def count_search_attempts(state: ResearchState, tree: ResearchTree) -> int:
    """Return created SEARCH attempts: final search experiments plus active SEARCH Plans."""
    settled = sum(
        1
        for experiment in tree.experiments(kind="search")
        if experiment.status in _TERMINAL
    )
    active = sum(1 for plan in state.plans.values() if plan.kind == "SEARCH")
    return settled + active


def _is_ready(turns_used: int, turn_limit: int | None) -> bool:
    """True when a Plan still has remaining turns or is unlimited."""
    return turn_limit is None or turns_used < turn_limit


class Scheduler:
    """Fill SEARCH concurrency slots in a fixed deterministic order."""

    def __init__(self, policy: HypothesisPolicy | None = None) -> None:
        self._policy = policy or EloPolicy()

    def seed(self, parent: Hypothesis | None) -> float:
        """Seed one hypothesis through the scheduler's policy."""
        return self._policy.seed(parent)

    def settle(self, reference_priority: float, outcome: Outcome) -> float:
        """Settle one hypothesis through the scheduler's policy."""
        return self._policy.settle(reference_priority, outcome)

    def next_actions(
        self,
        state: ResearchState,
        tree: ResearchTree,
        running_ids: Iterable[str],
        *,
        human_next: str | None = None,
        manual: bool = False,
    ) -> list[ScheduleAction]:
        """Project slot-filling actions without mutating state, tree, or policy.

        ``manual`` 模式下不按策略优先级自动出队：只在没有待选假设时请求生成
        候选，其余等待 ``human_next``（人工选定）再启一个。
        """
        if state.phase != "SEARCH":
            return []

        running = set(running_ids)
        free_slots = max(0, state.concurrency - len(running))
        actions: list[ScheduleAction] = []

        # 1. 先恢复已就绪且未运行的现有 Plan
        for plan_id, plan in state.plans.items():
            if free_slots == 0:
                break
            if (
                plan.kind == "SEARCH"
                and plan_id not in running
                and _is_ready(plan.turns_used, plan.turn_limit)
            ):
                actions.append(ScheduleAction.Resume(plan_id))
                free_slots -= 1

        create_budget = state.search_limit - count_search_attempts(state, tree)
        if create_budget > 0 and free_slots > 0:
            # 2. 用户单次点名的下一个假设，一次绕过策略队列但不改优先级
            if human_next is not None:
                actions.append(ScheduleAction.StartNextHypothesis(human_next))
                free_slots -= 1
                create_budget -= 1
            if manual:
                # 手动模式：跳过按优先级自动出队，仅当无待选假设时生成候选。
                if (
                    not tree.pending_hypotheses()
                    and free_slots > 0
                    and create_budget > 0
                ):
                    actions.append(
                        ScheduleAction.Generate(min(free_slots, create_budget))
                    )
            else:
                # 3. 策略优先级降序、FIFO 顺序升序的新假设队列
                for hypothesis in self._queued(tree, state, human_next):
                    if free_slots == 0 or create_budget == 0:
                        break
                    actions.append(ScheduleAction.StartNew(hypothesis.id))
                    free_slots -= 1
                    create_budget -= 1
                # 4. 剩余空位全部请求 SupervisorAgent 生成新假设
                if free_slots > 0 and create_budget > 0:
                    actions.append(
                        ScheduleAction.Generate(min(free_slots, create_budget))
                    )
        return actions

    def _queued(
        self, tree: ResearchTree, state: ResearchState, human_next: str | None
    ) -> list[Hypothesis]:
        candidates = [
            hypothesis
            for hypothesis in tree.pending_hypotheses()
            if hypothesis.id is not None
            and hypothesis.id not in state.plans
            and hypothesis.id != human_next
            and tree.experiment_for_hypothesis(hypothesis.id) is None
        ]
        return sorted(
            candidates,
            key=lambda hypothesis: queue_order(
                self._policy.priority(hypothesis, tree), hypothesis.order
            ),
        )

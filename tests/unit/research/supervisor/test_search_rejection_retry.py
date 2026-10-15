"""SEARCH candidate-quality rejections must be repairable and bounded.

A guard rejection (``no_change`` or ``diff_rejected``) means the turn produced
no measurable experiment.  Settling the Plan on it ends the candidate while the
Agent is still implementing, but retrying it forever lets a Plan that never
implements anything hold its slot.  These tests pin the retry allowance in both
directions, and the score-side reset that keeps the allowance about
"no measurement yet" rather than about total turns.
"""

import asyncio
from types import SimpleNamespace

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.supervisor.experiment import PlanBest, apply_trusted_score
from athena.research.supervisor.plans import (
    REJECTION_RETRY_LIMIT,
    PlanState,
    search_retry_note,
)
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.search_loop import SearchLoop
from athena.research.supervisor.state import ResearchState

_REF = "sha256:" + "a" * 64


class _PlanOwner:
    """Record the Plan operations the SEARCH loop performs."""

    def __init__(self) -> None:
        self.settled: list[tuple[str, object, object]] = []
        self.persisted = False

    async def persist_state(self) -> None:
        self.persisted = True

    async def publish_state(self) -> None:
        return None

    def save_state(self) -> None:
        return None

    async def settle_plan(self, plan_id, best_ref, result) -> None:
        self.settled.append((plan_id, best_ref, result))


def _loop(state: ResearchState, owner: _PlanOwner) -> SearchLoop:
    return SearchLoop(
        owner=SimpleNamespace(state=state, tree=SimpleNamespace()),
        deps=SimpleNamespace(),
        run=SupervisorRunState(lambda: state),
        plans=owner,
        run_turn=lambda _plan_id: asyncio.sleep(0),
    )


def _state(rejected_rounds: int = 0) -> ResearchState:
    return ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=1,
        concurrency=1,
        plans={
            "hyp_retry": PlanState(
                kind="SEARCH",
                context_ref=_REF,
                turns_used=1,
                turn_limit=12,
                patience=0,
                rejected_rounds=rejected_rounds,
            )
        },
    )


def _completed(kind: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan_id="hyp_retry",
        decision=SimpleNamespace(decision="continue", reason="still implementing"),
        result=SimpleNamespace(kind=kind, error="guard detail", next_state=None),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no_change", "diff_rejected"])
async def test_guard_rejection_retries_instead_of_settling(kind: str) -> None:
    """Neither rejection kind may settle a candidate that still has allowance."""

    state = _state()
    owner = _PlanOwner()

    await _loop(state, owner)._apply_completed_turn(_completed(kind))

    assert owner.settled == []
    assert owner.persisted is True
    assert state.plans["hyp_retry"].rejected_rounds == 1


@pytest.mark.asyncio
async def test_rejection_allowance_is_finite() -> None:
    """The last rejection settles the Plan so it releases its search slot."""

    state = _state(rejected_rounds=REJECTION_RETRY_LIMIT)
    owner = _PlanOwner()
    completed = _completed("no_change")

    await _loop(state, owner)._apply_completed_turn(completed)

    assert len(owner.settled) == 1
    plan_id, best_ref, result = owner.settled[0]
    assert plan_id == "hyp_retry"
    assert best_ref is None
    assert result is completed.result


@pytest.mark.asyncio
async def test_trusted_score_clears_the_rejection_allowance(tmp_path) -> None:
    """The allowance counts consecutive turns without a measurement."""

    store = LocalArtifactStore(tmp_path)
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=3,
        turn_limit=12,
        patience=2,
        rejected_rounds=4,
        stale_rounds=1,
    )

    scored = await apply_trusted_score(
        state,
        PlanBest(
            metric=0.81,
            commit="a" * 40,
            evidence_ref=_REF,
        ),
        store=store,
    )

    assert scored.rejected_rounds == 0
    assert scored.stale_rounds == 0
    assert scored.best_ref is not None


def test_retry_note_only_escalates_near_the_limit() -> None:
    """The Agent hears about the allowance only when it is about to run out."""

    assert search_retry_note(rejected_rounds=0) == ""
    assert search_retry_note(rejected_rounds=REJECTION_RETRY_LIMIT - 3) == ""

    near = search_retry_note(rejected_rounds=REJECTION_RETRY_LIMIT - 2)
    assert "2 rejected-turn allowance" in near

    spent = search_retry_note(rejected_rounds=REJECTION_RETRY_LIMIT + 1)
    assert "0 rejected-turn allowance" in spent

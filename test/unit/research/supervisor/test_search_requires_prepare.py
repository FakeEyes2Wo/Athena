"""SEARCH must refuse to start when PREPARE left nothing to measure against.

On 2026-09-02 a fresh TESS run reached SEARCH with no platform split, no frozen
evaluator and no baseline. The Supervisor had hit
``cannot propose SEARCH hypotheses without a SOTA`` during the
task-understanding turn, concluded it should establish the baseline itself, and
dispatched a General Agent to "build and validate a SOTA baseline". That agent's
shell is not sandboxed: it walked into a *different* project's directory, trained
on its 43,051-row split instead of this task's 507,789 rows, and reported PR-AUC
0.8668. The Supervisor believed it and moved on.

The first thing that noticed was the Ideator, several minutes later:

    SEARCH scheduling loop crashed:
    RuntimeError('EDA workspace not captured; PREPARE must run first')

which reads like a missing directory rather than a phase that never ran.
"""

from types import SimpleNamespace

import pytest

from athena.research.supervisor.search_loop import SearchLoop


class _Tree:
    def __init__(self, sota: str | None) -> None:
        self._sota = sota

    def best_experiment_id(self) -> str | None:
        return self._sota


class _Owner:
    def __init__(self, sota: str | None, plans: dict | None = None) -> None:
        self.tree = _Tree(sota)
        self.state = SimpleNamespace(status="RUNNING", plans=plans or {})


async def _never_run(_plan_id: str):  # pragma: no cover - the gate runs first
    raise AssertionError("no Plan turn may be dispatched before PREPARE finishes")


def _loop(*, sota: str | None, plans: dict | None = None) -> SearchLoop:
    return SearchLoop(
        _Owner(sota, plans),
        deps=None,
        run=None,
        plans=None,
        run_turn=_never_run,
    )


@pytest.mark.asyncio
async def test_search_refuses_to_run_without_a_trusted_baseline() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        await _loop(sota=None).run_search()

    detail = str(excinfo.value)
    assert "PREPARE did not finish" in detail
    assert "no trusted SOTA baseline" in detail
    # 说明为什么这件事致命，而不是只说缺了什么。
    assert "nothing to measure and nothing to compare" in detail


def test_a_finished_prepare_passes_the_gate() -> None:
    """The gate is a precondition, not a second scheduler."""
    _loop(sota="exp_baseline")._assert_prepare_finished()


def test_an_in_flight_plan_stands_the_gate_down() -> None:
    """``run_search`` is re-entered mid-SEARCH; a Plan proves PREPARE finished.

    ``_spawn_search`` re-enters the loop, and so does a resume that has to drain
    a crashed turn. Refusing there would turn a recoverable crash into a refusal.
    """
    _loop(sota=None, plans={"hyp_crash": object()})._assert_prepare_finished()


def test_a_seeded_sota_without_a_state_evaluator_ref_still_resumes() -> None:
    """The evaluator is recorded on the SOTA experiment, not on the state.

    An earlier version of this gate also required ``state.evaluator_ref``. A
    resumed run holds a seeded SOTA with that field unset, so the extra condition
    turned a valid resume into ``SEARCH/FAILED`` -- caught by
    ``test_resume_search_running_with_skip_completes_without_validation``.
    """
    loop = _loop(sota="exp_baseline")
    assert not hasattr(loop._state, "evaluator_ref")

    loop._assert_prepare_finished()

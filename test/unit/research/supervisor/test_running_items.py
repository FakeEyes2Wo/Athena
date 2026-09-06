"""SEARCH 必须能认出"刚跑完的这个 task 属于哪条 Plan"。

``running_tasks`` 给的是裸 Task。``search_loop`` 曾经对它做
``for item_id, item_task in self._run.running_tasks`` —— 把一个 Task 解包成两个
名字。这不会得到一条有用的报错：**已完成**的 asyncio Task 迭代出 0 个元素，于是
抛的是 ``not enough values to unpack (expected 2, got 0)``，栈还埋在生成器表达式里。

真机（2026-08-29）：SEARCH 每次有候选跑完就崩在这里。两个实验分别拿到 0.8523 和
0.8621，一次都没能结算——SEARCH 阶段实际上完全走不通。
"""

import asyncio
from types import SimpleNamespace

import pytest

from athena.research.supervisor.plans import PlanState
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.search_loop import SearchLoop
from athena.research.supervisor.state import ResearchState


def _run() -> SupervisorRunState:
    state = ResearchState(
        status="RUNNING", phase="SEARCH", search_limit=1, concurrency=1
    )
    return SupervisorRunState(lambda: state)


@pytest.mark.asyncio
async def test_running_items_pairs_each_plan_with_its_task() -> None:
    run = _run()

    async def work() -> str:
        return "done"

    task = asyncio.create_task(work())
    run.add_running("hyp_abc", task)
    await task

    assert run.running_items == (("hyp_abc", task),)
    found = next(pid for pid, t in run.running_items if t is task)
    assert found == "hyp_abc"


@pytest.mark.asyncio
async def test_unpacking_a_finished_task_is_the_trap_this_guards() -> None:
    """钉住那条报错的来历，免得有人再把 running_tasks 换回去。"""

    async def work() -> str:
        return "done"

    task = asyncio.create_task(work())
    await task

    with pytest.raises(ValueError, match="expected 2, got 0"):
        _a, _b = task


def test_running_items_is_empty_before_anything_starts() -> None:
    assert _run().running_items == ()


def test_run_status_and_kaggle_settings_come_from_research_state() -> None:
    state = ResearchState(
        status="RUNNING", phase="SEARCH", search_limit=1, concurrency=1
    )
    run = SupervisorRunState(lambda: state)

    state.status = "STOPPED"
    state.kaggle_download = False

    assert run.is_stopped() is True
    assert run.kaggle_enabled is True
    assert run.kaggle_download is False


@pytest.mark.asyncio
async def test_search_loop_dispatches_through_public_plan_turn_callback() -> None:
    state = ResearchState(
        status="RUNNING", phase="SEARCH", search_limit=1, concurrency=1
    )
    run = SupervisorRunState(lambda: state)
    completed = SimpleNamespace(plan_id="hyp_callback")

    async def run_turn(plan_id: str):
        assert plan_id == "hyp_callback"
        return completed

    loop = SearchLoop(
        owner=SimpleNamespace(state=state, tree=SimpleNamespace()),
        deps=SimpleNamespace(),
        run=run,
        plans=SimpleNamespace(),
        run_turn=run_turn,
    )

    loop._launch_turn("hyp_callback")
    [task] = run.running_tasks

    assert await task is completed


@pytest.mark.asyncio
async def test_search_loop_persists_through_public_plan_owner_surface() -> None:
    state = ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=1,
        concurrency=1,
        plans={
            "hyp_public": PlanState(
                kind="SEARCH",
                context_ref="sha256:" + "a" * 64,
                turns_used=1,
                turn_limit=2,
                patience=1,
            )
        },
    )
    run = SupervisorRunState(lambda: state)

    class PublicPlanOwner:
        persisted = False

        async def persist_state(self) -> None:
            self.persisted = True

    plans = PublicPlanOwner()
    loop = SearchLoop(
        owner=SimpleNamespace(state=state, tree=SimpleNamespace()),
        deps=SimpleNamespace(),
        run=run,
        plans=plans,
        run_turn=lambda _plan_id: asyncio.sleep(0),
    )

    await loop._apply_completed_turn(
        SimpleNamespace(plan_id="hyp_public", decision=None, result=None)
    )

    assert plans.persisted is True

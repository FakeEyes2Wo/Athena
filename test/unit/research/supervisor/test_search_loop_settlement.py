"""SEARCH 必须能结算一个跑完的 plan。

``SupervisorRunState.running_tasks`` 返回的是 Task 元组，而 ``run_search`` 曾按
``(item_id, item_task)`` 解包它。解包会去迭代 ``asyncio.Task``（Future 的
``__iter__`` 即 ``__await__``），而**已完成**的 Task 迭代结果为空——而
``asyncio.wait`` 返回的恰恰是已完成的 task。于是第一个 plan 跑完就抛
``ValueError: not enough values to unpack (expected 2, got 0)``，整个 SEARCH
从来没有结算过哪怕一个 plan。2026-08-29 真机两次复现。
"""

import asyncio
from types import SimpleNamespace

import pytest

from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.search_loop import SearchLoop


def _loop_with_one_finished_plan(
    task: asyncio.Task,
) -> tuple[SearchLoop, list, SupervisorRunState]:
    """一台只跑「结算一个已完成 plan」这一步的 SearchLoop。"""
    run = SupervisorRunState(kaggle_download=None)
    run.add_running("hyp_1", task)
    published: list[str] = []

    async def publish_state():
        published.append("state")

    loop = SearchLoop(
        owner=SimpleNamespace(
            state=SimpleNamespace(status="RUNNING", phase="SEARCH", plans={}),
            _publish_state=publish_state,
        ),
        deps=SimpleNamespace(),
        run=run,
        plans=SimpleNamespace(),
    )
    return loop, published, run


@pytest.mark.asyncio
async def test_search_settles_a_finished_plan() -> None:
    """跑完的 plan 要能被认领、出队并交给结算。"""

    async def turn():
        return SimpleNamespace(plan_id="hyp_1", result=None)

    task = asyncio.create_task(turn())
    await task
    loop, _published, run = _loop_with_one_finished_plan(task)

    settled: list[object] = []

    async def fill_slots() -> bool:
        return False

    async def apply_completed_turn(completed) -> None:
        settled.append(completed)

    async def wait_for_manual_selection() -> bool:
        return False

    loop._fill_slots = fill_slots
    loop._apply_completed_turn = apply_completed_turn
    loop._wait_for_manual_selection = wait_for_manual_selection

    await loop.run_search()

    assert [c.plan_id for c in settled] == ["hyp_1"]
    assert tuple(run.running_ids()) == ()


@pytest.mark.asyncio
async def test_search_settles_the_right_plan_among_several() -> None:
    """多个在跑时，认领的必须是真正完成的那一个。"""

    async def finished():
        return SimpleNamespace(plan_id="hyp_2", result=None)

    async def pending():
        await asyncio.Event().wait()

    done_task = asyncio.create_task(finished())
    await done_task
    slow_task = asyncio.create_task(pending())

    run = SupervisorRunState(kaggle_download=None)
    run.add_running("hyp_1", slow_task)
    run.add_running("hyp_2", done_task)

    async def publish_state():
        return None

    loop = SearchLoop(
        owner=SimpleNamespace(
            state=SimpleNamespace(status="RUNNING", phase="SEARCH", plans={}),
            _publish_state=publish_state,
        ),
        deps=SimpleNamespace(),
        run=run,
        plans=SimpleNamespace(),
    )
    settled: list[object] = []

    async def fill_slots() -> bool:
        return False

    async def apply_completed_turn(completed) -> None:
        settled.append(completed)
        run.set_stopped(True)

    loop._fill_slots = fill_slots
    loop._apply_completed_turn = apply_completed_turn

    await loop.run_search()

    assert [c.plan_id for c in settled] == ["hyp_2"]
    # 只出队完成的那个，未完成的仍在册。
    assert tuple(run.running_ids()) == ("hyp_1",)
    slow_task.cancel()
    await asyncio.gather(slow_task, return_exceptions=True)


def test_running_items_pairs_ids_with_tasks() -> None:
    """``running_items`` 是 (plan_id, task) 对；``running_tasks`` 仍只给 task。"""
    run = SupervisorRunState(kaggle_download=None)

    async def make() -> asyncio.Task:
        return asyncio.create_task(asyncio.sleep(0))

    task = asyncio.new_event_loop().run_until_complete(make())
    run.add_running("hyp_1", task)

    assert tuple(run.running_items) == (("hyp_1", task),)
    assert run.running_tasks == (task,)

"""ResearchRuntime ownership, lifecycle, persistence, and routing tests."""

import asyncio

import pytest

from athena.core.research_tree import ResearchTree
from athena.research import (
    ResearchMethod,
    ResearchRuntime,
    ResearchWorkflowDependencies,
)


def _task_params() -> dict[str, object]:
    return {
        "task_type": "classification",
        "data_type": "tabular",
        "target_vars": ["label"],
        "primary_metric": "f1_macro",
        "direction": "maximize",
    }


@pytest.mark.asyncio
async def test_runtime_owns_state_and_configures_task() -> None:
    runtime = ResearchRuntime()
    result = await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    assert result["configured"] is True
    assert runtime.phase == "CONFIGURED"
    for attribute in (
        "_tree",
        "_phase",
        "_active_task",
        "_budget",
        "_run_task",
        "_resume_gate",
        "_subscribers",
    ):
        assert hasattr(runtime, attribute)
    with pytest.raises(ValueError, match="unknown method"):
        await runtime.dispatch("unknown_research_method", {})


@pytest.mark.asyncio
async def test_tree_save_and_strict_load_replace_state(tmp_path) -> None:
    path = tmp_path / "tree.json"
    runtime = ResearchRuntime(save_path=path)
    saved = await runtime.dispatch(ResearchMethod.TREE_SAVE, {})
    loaded = await runtime.dispatch(ResearchMethod.TREE_LOAD, {})
    assert saved == {"saved": True, "path": str(path)}
    assert loaded["tree"] == {
        "version": 2,
        "sota_id": None,
        "hypotheses": {},
        "experiments": {},
    }


class BlockingSearch:
    def __init__(self, checkpoint) -> None:
        self.checkpoint = checkpoint
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self):
        self.started.set()
        await self.checkpoint()
        await self.release.wait()
        return []


@pytest.mark.asyncio
async def test_search_task_pause_resume_stop_and_events() -> None:
    searches: list[BlockingSearch] = []

    async def prepare(task, tree):
        return "exp-baseline"

    def search_factory(tree, budget, checkpoint):
        search = BlockingSearch(checkpoint)
        searches.append(search)
        return search

    runtime = ResearchRuntime(
        dependencies=ResearchWorkflowDependencies(
            prepare_baseline=prepare,
            search_factory=search_factory,
        )
    )
    events = []
    runtime.subscribe(lambda kind, data: events.append((kind, data)))
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    started = await runtime.dispatch(
        ResearchMethod.SEARCH_START, {"max_experiments": 3}
    )
    for _ in range(10):
        if searches:
            break
        await asyncio.sleep(0)
    assert searches
    await asyncio.wait_for(searches[0].started.wait(), timeout=1)
    assert started["status"] == "started"
    assert runtime.phase == "SEARCH"
    assert runtime.run_task is not None
    await runtime.dispatch(ResearchMethod.SEARCH_PAUSE, {})
    assert runtime.phase == "PAUSED"
    await runtime.dispatch(ResearchMethod.SEARCH_RESUME, {})
    assert runtime.phase == "SEARCH"
    stopped = await runtime.dispatch(ResearchMethod.SEARCH_STOP, {})
    assert stopped["status"] == "stopped"
    assert runtime.phase == "CANCELLED"
    assert runtime.run_task is None
    assert any(kind == "phase/change" for kind, _ in events)


@pytest.mark.asyncio
async def test_runtime_rejects_missing_backends_and_invalid_phases() -> None:
    runtime = ResearchRuntime()
    with pytest.raises(RuntimeError, match="configured task"):
        await runtime.dispatch(ResearchMethod.SEARCH_START, {})
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    with pytest.raises(RuntimeError, match="search backend"):
        await runtime.dispatch(ResearchMethod.SEARCH_START, {})
    with pytest.raises(RuntimeError, match="SEARCH phase"):
        await runtime.dispatch(ResearchMethod.SEARCH_PAUSE, {})


@pytest.mark.asyncio
async def test_validation_and_reporting_route_to_dependencies() -> None:
    tree = ResearchTree.load("test/fixtures/research_tree_v2.json")

    class Validator:
        async def run(self, sota_id, supplied_tree):
            assert supplied_tree is tree
            return type(
                "Result",
                (),
                {"model_dump": lambda self, **kwargs: {"sota_id": sota_id}},
            )()

    class Reporter:
        async def generate(self, sota_id, supplied_tree):
            assert supplied_tree is tree
            return "artifact://reports/final.md"

    runtime = ResearchRuntime(
        tree=tree,
        dependencies=ResearchWorkflowDependencies(
            validator=Validator(), reporter=Reporter()
        ),
    )
    validation = await runtime.dispatch(ResearchMethod.VALIDATE_START, {})
    report = await runtime.dispatch(ResearchMethod.REPORT_GENERATE, {})
    assert validation == {"sota_id": "exp_baseline"}
    assert report == {"report_ref": "artifact://reports/final.md"}
    assert runtime.phase == "COMPLETED"

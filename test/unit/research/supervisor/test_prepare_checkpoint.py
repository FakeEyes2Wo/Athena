"""PREPARE 断点：进度经单写者落盘，续跑时据此跳过已完成的步骤。

PREPARE 是两个串行步骤（冻结 evaluator → 跑 baseline 并可信打分）。此前它是唯一
没有 checkpoint 的阶段：SEARCH 有 ``state.plans`` + ``recover()``，VALIDATE 有
``state.validation`` 的 ``result_ref``，而 PREPARE 每次都从头再来一遍。
"""

from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_tree import ResearchTree
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor


async def _make_supervisor(tmp_path: Path) -> Supervisor:
    """Build a PREPARE-phase Supervisor with no tree and no plans."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    state = ResearchState(
        status="RUNNING", phase="PREPARE", search_limit=10, concurrency=1
    )

    async def unused_plan_turn(_plan_id, _state):
        raise AssertionError("Plan turn must not run")

    async def unused_supervisor_turn(_text):
        raise AssertionError("Supervisor turn must not run")

    async def publish(_kind, _payload):
        return None

    return Supervisor(
        project_root=tmp_path,
        state_root=tmp_path / ".athena",
        state=state,
        tree=ResearchTree(),
        store=store,
        agents=None,
        workspaces=None,
        scheduler=Scheduler(),
        recovery=Recovery(),
        evaluator_ref=None,
        run_plan_turn=unused_plan_turn,
        run_supervisor_turn=unused_supervisor_turn,
        publish=publish,
    )


@pytest.mark.asyncio
async def test_checkpoint_prepare_persists_progress(tmp_path: Path) -> None:
    """冻结好的 evaluator 引用要落进 state.json，重开进程读得回来。"""
    supervisor = await _make_supervisor(tmp_path)

    await supervisor.checkpoint_prepare(evaluator_ref="sha256:eval")

    reloaded = ResearchState.load(tmp_path / ".athena" / "state.json")
    assert reloaded.prepare == {"evaluator_ref": "sha256:eval"}


@pytest.mark.asyncio
async def test_checkpoint_prepare_merges_later_fields(tmp_path: Path) -> None:
    """后续 checkpoint 只补字段，不能把先前记下的 evaluator_ref 抹掉。"""
    supervisor = await _make_supervisor(tmp_path)

    await supervisor.checkpoint_prepare(evaluator_ref="sha256:eval")
    await supervisor.checkpoint_prepare(prepare_turns=2)

    assert supervisor.state.prepare == {
        "evaluator_ref": "sha256:eval",
        "prepare_turns": 2,
    }

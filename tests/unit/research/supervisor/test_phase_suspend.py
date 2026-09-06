"""``Supervisor.suspend()`` 的运行态降级契约。

``stop()`` 从不写 ``state.status``，所以关掉一个正在跑的 runtime 会在磁盘上留下
``status="RUNNING"`` 却没有任何进程在跑的悬空态。``suspend()`` 负责在拆卸之前把它
落成 ``WAITING``。
"""

from pathlib import Path

import pytest

from tests.support import RecordingDocumentProjector

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_tree import ResearchTree
from athena.research.supervisor.deps import (
    PhaseActions,
    ResearchActions,
    SearchServices,
    SupervisorDeps,
    SupervisorPaths,
    SupervisorRuntime,
)
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduling import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor


def _supervisor(tmp_path: Path, *, status: str) -> Supervisor:
    """构造一个只有真实状态文件、没有活体 Agent 的 Supervisor。"""

    async def unused_plan_turn(_plan_id, _state):
        raise AssertionError("Plan turn must not run")

    async def unused_supervisor_turn(_text):
        raise AssertionError("Supervisor turn must not run")

    async def publish(_kind, _payload):
        return None

    state = ResearchState(
        status=status, phase="PREPARE", search_limit=10, concurrency=4
    )
    return Supervisor(
        state=state,
        tree=ResearchTree(),
        deps=SupervisorDeps(
            paths=SupervisorPaths(
                project_root=tmp_path,
                state_path=tmp_path / ".athena" / "state.json",
                tree_path=tmp_path / ".athena" / "research_tree.json",
            ),
            runtime=SupervisorRuntime(
                store=LocalArtifactStore(tmp_path / "artifacts"),
                agents=None,
                workspaces=None,
                documents=RecordingDocumentProjector(),
            ),
            research=ResearchActions(
                plan=unused_plan_turn, supervisor=unused_supervisor_turn
            ),
            phases=PhaseActions(publish=publish),
            search=SearchServices(scheduler=Scheduler(), recovery=Recovery()),
        ),
    )


@pytest.mark.asyncio
async def test_suspend_downgrades_running_to_waiting_on_disk(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, status="RUNNING")

    assert await supervisor.suspend() == "WAITING"

    assert supervisor.state.status == "WAITING"
    reloaded = ResearchState.load(tmp_path / ".athena" / "state.json")
    assert reloaded.status == "WAITING"
    assert reloaded.phase == "PREPARE"


@pytest.mark.asyncio
async def test_suspend_leaves_an_explicit_stop_untouched(tmp_path: Path) -> None:
    """STOPPED 是人类的显式决定：suspend 不得改写它，也不该多写一次盘。"""
    supervisor = _supervisor(tmp_path, status="STOPPED")

    assert await supervisor.suspend() == "STOPPED"

    assert supervisor.state.status == "STOPPED"
    assert not (tmp_path / ".athena" / "state.json").exists()

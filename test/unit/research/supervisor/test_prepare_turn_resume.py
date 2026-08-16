"""PREPARE 轮次级续跑：重启不重置轮次预算，也不把原始任务再发一遍。

对话记忆本来就由 rollout 自动恢复（见 ``RuntimeThreadManager._make_runtime``），
所以重启后真正丢的是编排状态：``for turn in range(max_turns)`` 从 0 重来，且
``create_root`` 会把原始任务当作新一轮重新发出去——让 agent 重做它在自己 transcript
里看得见已经做完的事。续跑要做两件事：从断点处的轮次接着数，首轮用 followup 接续。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.supervisor.prepare import (
    EVALUATOR_AGENT_ID,
    run_evaluator_plan,
    run_prepare_plan,
)


class _Agents:
    """AgentRuntime 替身：记录调度动作；每轮都返回一个无效结果。

    无效结果让 ``run_evaluator_plan`` 走"转成反馈重试"的分支，从而把轮次预算跑干，
    于是"跑了几轮"可以被精确断言，无需在磁盘上摆出一个真的可冻结 evaluator。
    """

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.runs = 0

    async def create_root(self, agent_type: str, _task: Any, **kwargs: Any):
        self.actions.append("create_root")
        return kwargs.get("agent_id") or agent_type, "run-0"

    async def resume_agent(self, _agent_id: str, **_kwargs: Any) -> None:
        self.actions.append("resume_agent")

    async def followup(self, _agent_id: str, _task: Any) -> str:
        self.actions.append("followup")
        return f"run-{len(self.actions)}"

    async def wait_run(self, _run_id: str):
        self.runs += 1
        return SimpleNamespace(status="FAILED", response_ref=None, error="boom")


async def _run(
    tmp_path: Path, agents: _Agents, *, turns_used: int, max_turns: int, checkpoints: list
) -> None:
    """Drive the evaluator step until its turn budget is exhausted."""

    async def checkpoint(turns: int) -> None:
        checkpoints.append(turns)

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_evaluator_plan(
            agents=agents,
            scripts=object(),
            store=LocalArtifactStore(tmp_path / "artifacts"),
            evaluator_dir=tmp_path / "evaluator",
            execution=SimpleNamespace(ensure_environment=lambda: None),
            task="build an evaluator",
            max_turns=max_turns,
            turns_used=turns_used,
            checkpoint=checkpoint,
        )


@pytest.mark.asyncio
async def test_resume_spends_only_the_remaining_turns(tmp_path: Path) -> None:
    """断点处已用掉 2 轮、上限 3 轮 → 只剩 1 轮可跑，不是重新来 3 轮。"""
    agents = _Agents()
    checkpoints: list[int] = []

    await _run(tmp_path, agents, turns_used=2, max_turns=3, checkpoints=checkpoints)

    assert agents.runs == 1


@pytest.mark.asyncio
async def test_resume_continues_the_session_instead_of_resending_the_task(
    tmp_path: Path,
) -> None:
    """续跑先恢复会话再 followup，绝不用 create_root 把原始任务重发一遍。"""
    agents = _Agents()

    await _run(tmp_path, agents, turns_used=2, max_turns=3, checkpoints=[])

    assert "create_root" not in agents.actions
    assert agents.actions[:2] == ["resume_agent", "followup"]


@pytest.mark.asyncio
async def test_fresh_run_creates_the_agent_with_the_task(tmp_path: Path) -> None:
    """全新运行仍走 create_root，且把两轮预算跑满。"""
    agents = _Agents()

    await _run(tmp_path, agents, turns_used=0, max_turns=2, checkpoints=[])

    assert agents.actions[0] == "create_root"
    assert "resume_agent" not in agents.actions
    assert agents.runs == 2


@pytest.mark.asyncio
async def test_each_turn_checkpoints_the_cumulative_count(tmp_path: Path) -> None:
    """每轮结束记一次累计轮次，崩溃循环才不会无限烧预算。"""
    agents = _Agents()
    checkpoints: list[int] = []

    await _run(tmp_path, agents, turns_used=1, max_turns=3, checkpoints=checkpoints)

    assert checkpoints == [2, 3]


def test_evaluator_agent_id_is_stable() -> None:
    """恢复会话按固定 agent_id 寻址 rollout，这个常量不能漂。"""
    assert EVALUATOR_AGENT_ID == "evaluator"


# ── 步骤 2：baseline agent 的同一套续跑语义 ──────────────────────────────


async def _run_baseline(
    tmp_path: Path, agents: _Agents, *, turns_used: int, max_turns: int
) -> None:
    """Drive the baseline step until its turn budget is exhausted."""
    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_prepare_plan(
            agents=agents,
            evaluator=object(),
            git=object(),
            workspace=SimpleNamespace(path=str(tmp_path / "eda")),
            execution=SimpleNamespace(ensure_environment=lambda: None),
            store=LocalArtifactStore(tmp_path / "artifacts"),
            evaluator_ref="sha256:eval",
            tree_ref="sha256:tree",
            task="build a baseline",
            max_turns=max_turns,
            turns_used=turns_used,
        )


@pytest.mark.asyncio
async def test_baseline_resume_spends_only_the_remaining_turns(tmp_path: Path) -> None:
    """baseline 步骤同样从断点处的轮次接着数。"""
    agents = _Agents()

    await _run_baseline(tmp_path, agents, turns_used=2, max_turns=3)

    assert agents.runs == 1


@pytest.mark.asyncio
async def test_baseline_resume_continues_the_session(tmp_path: Path) -> None:
    """baseline 步骤续跑也走 resume_agent + followup，不重发原始任务。"""
    agents = _Agents()

    await _run_baseline(tmp_path, agents, turns_used=2, max_turns=3)

    assert "create_root" not in agents.actions
    assert agents.actions[:2] == ["resume_agent", "followup"]


@pytest.mark.asyncio
async def test_baseline_fresh_run_creates_the_agent(tmp_path: Path) -> None:
    """全新运行仍走 create_root。"""
    agents = _Agents()

    await _run_baseline(tmp_path, agents, turns_used=0, max_turns=2)

    assert agents.actions[0] == "create_root"
    assert agents.runs == 2

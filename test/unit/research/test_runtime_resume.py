"""断点续传：重开进程接着跑时，不应把已经做完的任务理解再做一遍。

任务理解是 PREPARE 之前的一次性 turn（决定是否接入 Kaggle 工具），其结构化结果
由 ``record_task_understanding`` 落进 ``state.task_understanding``。该字段非空即
表示上一轮已经理解过任务，续跑时必须跳过。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athena.research.runtime import ResearchRuntime


class _RecordingTurns:
    """AgentTurnRunner 替身：记录每次 supervisor turn 的输入。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_supervisor_turn(self, text: str) -> str:
        self.prompts.append(text)
        return "ok"


class _RecordingEvents:
    """RuntimeEvents 替身：只收集发布出去的文本，不落盘。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def replay_output_events(self) -> list[dict[str, object]]:
        return []

    async def publish_output(self, **payload: Any) -> None:
        self.texts.append(str(payload.get("text", "")))


def _runtime(
    *,
    task_understanding: dict[str, object] | None,
    status: str = "RUNNING",
    state_path: Path | None = None,
) -> ResearchRuntime:
    """组装一个只够跑通 ``start()`` 的 runtime：其余依赖全部替身化。

    例：``_runtime(task_understanding={"title": "t"})`` → ``start()`` 后
    ``runtime._agent_turns.prompts == []``（已理解过，不再重跑）。
    """
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._state_path = state_path or Path("state.json")
    runtime._root = Path(".")
    runtime._task = None
    runtime._started = False
    runtime._task_text = "参加 kaggriculture 比赛"
    runtime._provider = object()
    runtime._survey_enabled = False
    runtime._survey_task = None
    runtime._agent_turns = _RecordingTurns()
    runtime._events_bus = _RecordingEvents()
    runtime._agents = SimpleNamespace(start=lambda: None)

    async def init(**_kwargs: Any) -> str:
        return "commit0"

    runtime._git = SimpleNamespace(init=init)

    starts: list[str] = []

    async def supervisor_start() -> None:
        starts.append("start")

    async def supervisor_resume() -> str:
        runtime._supervisor.state.status = "RUNNING"
        return "RUNNING"

    runtime._supervisor = SimpleNamespace(
        state=SimpleNamespace(
            phase="PREPARE",
            status=status,
            task_understanding=task_understanding,
            corpus_ref=None,
        ),
        start=supervisor_start,
        starts=starts,
        resume=supervisor_resume,
        tree=SimpleNamespace(best_experiment_id=lambda: None),
    )
    runtime._state = runtime._supervisor.state
    runtime._tree = runtime._supervisor.tree
    return runtime


@pytest.mark.asyncio
async def test_resume_skips_task_understanding_already_recorded() -> None:
    """state 里已有任务理解 → 续跑时不再发起任务理解 turn。"""
    runtime = _runtime(task_understanding={"title": "Kaggriculture", "target": "income"})

    task = await runtime.start()
    await task

    assert runtime._agent_turns.prompts == []


@pytest.mark.asyncio
async def test_fresh_run_still_performs_task_understanding() -> None:
    """全新项目（state 里没有任务理解）→ 仍要跑一次任务理解 turn。"""
    runtime = _runtime(task_understanding=None)

    task = await runtime.start()
    await task

    assert len(runtime._agent_turns.prompts) == 1
    assert "kaggriculture" in runtime._agent_turns.prompts[0]


@pytest.mark.asyncio
async def test_resume_restarts_an_interrupted_prepare(tmp_path: Path) -> None:
    """PREPARE 中途断掉、进程重开后 ``/resume`` 必须真的把阶段跑起来。

    这时还没有 baseline（SOTA 由 PREPARE 产出），旧的 ``_ensure_started`` 只认
    baseline，于是 ``/resume`` 只把 status 改成 RUNNING 就返回——界面显示"运行中"，
    实际什么都没跑。判据应是"磁盘上有 state.json"（此前启动过），而非有没有 baseline。
    """
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    runtime = _runtime(
        task_understanding={"title": "Kaggriculture"},
        status="WAITING",
        state_path=state_path,
    )

    await runtime.message("/resume")
    if runtime._task is not None:
        await runtime._task

    assert runtime._supervisor.starts == ["start"]

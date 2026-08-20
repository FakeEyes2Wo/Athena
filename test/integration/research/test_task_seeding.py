"""ResearchRuntime 任务 seed：auto_seed_task（TUI 入口）首条普通消息 seed 为
research task 并从 PREPARE 启动，避免无任务直接进入 SEARCH（无 baseline/SOTA）；
默认（A.2 测试）message 保持纯 supervisor 委托。"""

import asyncio
import contextlib
from pathlib import Path

import pytest

from athena.research import ResearchRuntime
from athena.research.rubrics.models import EvaluationPolicy


def _make_runtime(tmp_path: Path, *, auto_seed_task: bool = False) -> ResearchRuntime:
    """默认 runtime 从 SEARCH 开始；TUI auto-seed runtime 等待 PREPARE 任务。

    Task Understanding 与 Rubric V2 用 fake 结果，随后 PREPARE 的真实 agent 才在
    fake client 上失败；测试保持 hermetic，不 hit 真实 API。
    """
    runtime = ResearchRuntime(
        project_root=tmp_path,
        model="fake",
        client=object(),
        auto_seed_task=auto_seed_task,
    )

    async def fake_task_understanding(_text: str) -> str:
        runtime.state.task_understanding = {
            "title": "test task",
            "dataset": "",
            "target": "",
            "task_type": "other",
            "primary_metric": None,
            "direction": None,
            "metric_source": "unresolved",
            "evaluation_plan": "",
        }
        return "understood"

    async def fake_evaluation_rubric():
        policy = EvaluationPolicy(
            primary_metric="accuracy",
            direction="maximize",
            metric_source="ai",
            locked=False,
            confidence=0.5,
            explanation="Hermetic test policy.",
        )
        ref = await runtime._store.put_text(policy.model_dump_json())
        return policy, ref

    runtime._agent_turns.run_supervisor_turn = fake_task_understanding  # type: ignore[method-assign]
    runtime._agent_turns.run_evaluation_rubric = fake_evaluation_rubric  # type: ignore[method-assign]
    return runtime


async def _close(runtime: ResearchRuntime) -> None:
    task = runtime._task
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await task
    await runtime.aclose()


@pytest.mark.asyncio
async def test_fresh_runtime_starts_in_search_phase(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        assert runtime.state.phase == "SEARCH"
        assert runtime._started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_start_task_explicit_seeds_prepare_and_starts(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        status = await runtime.start_task("predict titanic survival")
        assert status == "RUNNING"
        assert runtime._task_text == "predict titanic survival"
        assert runtime._started is True
        assert runtime.state.phase == "PREPARE"
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_auto_seed_task_first_message_starts_prepare(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, auto_seed_task=True)
    try:
        status = await runtime.message("predict titanic survival")
        assert status == "RUNNING"
        assert runtime._task_text == "predict titanic survival"
        assert runtime._started is True
        assert runtime.state.phase == "PREPARE"
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_auto_seed_runtime_reports_prepare_before_first_message(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, auto_seed_task=True)
    seen: list[tuple[str, dict[str, object]]] = []
    try:
        runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

        assert runtime.state.phase == "PREPARE"
        assert seen[0][0] == "state"
        assert seen[0][1]["phase"] == "PREPARE"
        assert runtime._started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_auto_seed_task_skips_control_commands(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, auto_seed_task=True)
    try:
        status = await runtime.message("/pause")
        assert status == "WAITING"
        assert runtime._task_text == ""
        assert runtime._started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_message_default_does_not_seed(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        status = await runtime.message("/pause")
        assert status == "WAITING"
        assert runtime._task_text == ""
        assert runtime._started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_start_task_reuses_persisted_task_understanding(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        runtime.state.task_understanding = {"title": "titanic"}
        runtime.state.task_text = "predict titanic survival"
        runs: list[str] = []

        async def fake_supervisor_turn(text: str) -> str:
            runs.append(text)
            return "should not run"

        runtime._agent_turns.run_supervisor_turn = fake_supervisor_turn  # type: ignore[method-assign]
        events: list[tuple[str, dict[str, object]]] = []
        runtime.subscribe(lambda kind, payload: events.append((kind, payload)))

        status = await runtime.start_task("continue")

        assert status == "RUNNING"
        assert runtime._task_text == "predict titanic survival"
        assert runs == []
        assert any(
            kind == "output"
            and "断点续传：复用已持久化的任务理解" in str(payload.get("text"))
            for kind, payload in events
        )
        assert not any(
            kind == "output" and "任务理解中" in str(payload.get("text"))
            for kind, payload in events
        )
    finally:
        await _close(runtime)

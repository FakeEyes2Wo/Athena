"""ResearchRuntime 任务 seed：auto_seed_task（TUI 入口）首条普通消息 seed 为
research task 并从 PREPARE 启动，避免无任务直接进入 SEARCH（无 baseline/SOTA）；
默认（A.2 测试）message 保持纯 supervisor 委托。"""

import asyncio
import contextlib
from pathlib import Path

import pytest

from athena.research import ResearchRuntime


def _make_runtime(tmp_path: Path, *, auto_seed_task: bool = False) -> ResearchRuntime:
    """默认 runtime 从 SEARCH 开始；TUI auto-seed runtime 等待 PREPARE 任务。

    ``client=object()`` 让后台 supervisor 的 PREPARE 在首次 LLM 调用即失败，
    保证测试 hermetic（不 hit 真实 API、不依赖 git 执行结果）。
    """
    return ResearchRuntime(
        project_root=tmp_path,
        model="fake",
        client=object(),
        auto_seed_task=auto_seed_task,
    )


async def _close(runtime: ResearchRuntime) -> None:
    task = runtime._task
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await task
    await runtime.aclose()


@pytest.mark.asyncio
async def test_fresh_runtime_is_idle_at_prepare_until_seeded(tmp_path: Path) -> None:
    """没有任务的全新 runtime 不得谎报运行中，也不得直接落在 SEARCH。

    GUI 网关正是这样构造 runtime 的：一连上就把首帧 state 推给前端。首帧若说
    ``RUNNING``/``SEARCH``，前端会以为已有运行在进行中，从而把首条任务路由到
    supervisor 对话而不是 ``start_search``，阶段机永远不启动。
    """
    runtime = _make_runtime(tmp_path)
    try:
        assert runtime.state.status == "IDLE"
        assert runtime.state.phase == "PREPARE"
        assert runtime._started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_idle_runtime_reports_idle_in_its_first_state_event(tmp_path: Path) -> None:
    """订阅即收到的首帧必须同样是 IDLE（GUI 首帧就取自这里）。"""
    runtime = _make_runtime(tmp_path)
    seen: list[tuple[str, dict[str, object]]] = []
    try:
        runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

        assert seen[0][0] == "state"
        assert seen[0][1]["status"] == "IDLE"
        assert seen[0][1]["phase"] == "PREPARE"
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

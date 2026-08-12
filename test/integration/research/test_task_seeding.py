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

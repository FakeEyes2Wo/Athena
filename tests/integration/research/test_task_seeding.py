"""ResearchRuntime 任务 seed：auto_seed_task（TUI 入口）首条普通消息 seed 为
research task 并从 PREPARE 启动，避免无任务直接进入 SEARCH（无 baseline/SOTA）；
默认（A.2 测试）message 保持纯 supervisor 委托。"""

import asyncio
import contextlib
from pathlib import Path

import pytest

from athena.research import ResearchRuntime
from athena.research.config import (
    ProviderConfig,
    ResearchOptions,
    RuntimeAdapters,
    RuntimeDependencies,
    TaskConfig,
)
from athena.research.runtime.resume_contract import ResearchControlError


def _make_runtime(tmp_path: Path, *, auto_seed_task: bool = False) -> ResearchRuntime:
    """默认 runtime 以 IDLE/PREPARE 等待任务；TUI auto-seed runtime 也等待 PREPARE。

    ``client=object()`` 让后台 supervisor 的 PREPARE 在首次 LLM 调用即失败，
    保证测试 hermetic（不 hit 真实 API、不依赖 git 执行结果）。
    """
    return ResearchRuntime(
        project_root=tmp_path,
        research=ResearchOptions(
            task=TaskConfig(auto_seed=auto_seed_task, auto_confirm=True)
        ),
        dependencies=RuntimeDependencies(
            provider=ProviderConfig(model="fake", client=object())
        ),
    )


def _prepare_runtime(tmp_path: Path, prepare) -> ResearchRuntime:
    return ResearchRuntime(
        project_root=tmp_path,
        research=ResearchOptions(task=TaskConfig(auto_confirm=True)),
        dependencies=RuntimeDependencies(adapters=RuntimeAdapters(prepare=prepare)),
    )


async def _close(runtime: ResearchRuntime) -> None:
    task = runtime.session.lifecycle.task
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await task
    await runtime.aclose()


@pytest.mark.asyncio
async def test_fresh_runtime_starts_idle_in_prepare_phase(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        assert runtime.state.phase == "PREPARE"
        assert runtime.state.status == "IDLE"
        assert runtime.session.lifecycle.started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_start_task_explicit_seeds_prepare_and_starts(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        status = await runtime.start_task("predict titanic survival")
        assert status == "RUNNING"
        assert runtime.task_text == "predict titanic survival"
        assert runtime.session.lifecycle.started is True
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
        assert runtime.task_text == "predict titanic survival"
        assert runtime.session.lifecycle.started is True
        assert runtime.state.phase == "PREPARE"
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_auto_seed_exact_continue_rejects_without_creating_a_task(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, auto_seed_task=True)
    try:
        with pytest.raises(ResearchControlError) as caught:
            await runtime.message("  Continue  ")

        assert caught.value.code == "resume_unavailable"
        assert runtime.state.task_text is None
        assert runtime.state.task_understanding is None
        assert runtime.clarification_path.exists() is False
        assert runtime.session.lifecycle.started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_resume_aliases_use_the_same_public_runtime_primitive(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path)
    calls: list[str] = []

    async def fake_resume() -> str:
        calls.append("resume")
        return "RUNNING"

    runtime.resume_current_task = fake_resume  # type: ignore[method-assign]
    try:
        assert await runtime.message("/resume") == "RUNNING"
        assert await runtime.message("continue") == "RUNNING"
        assert await runtime.start_task("CONTINUE") == "RUNNING"
        assert calls == ["resume", "resume", "resume"]
        assert runtime.state.task_understanding is None
        assert runtime.clarification_path.exists() is False
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
        assert runtime.session.lifecycle.started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_auto_seed_task_skips_control_commands(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, auto_seed_task=True)
    try:
        status = await runtime.message("/pause")
        assert status == "WAITING"
        assert runtime.task_text == ""
        assert runtime.session.lifecycle.started is False
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_message_default_does_not_seed(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        status = await runtime.message("/pause")
        assert status == "WAITING"
        assert runtime.task_text == ""
        assert runtime.session.lifecycle.started is False
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

        agent_turns = runtime.services.workflow.agent_turns
        agent_turns.run_supervisor_turn = fake_supervisor_turn  # type: ignore[method-assign]
        events: list[tuple[str, dict[str, object]]] = []
        runtime.subscribe(lambda kind, payload: events.append((kind, payload)))

        status = await runtime.start_task("continue")

        assert status == "RUNNING"
        assert runtime.task_text == "predict titanic survival"
        assert runs == []
        # The inline task-understanding turn is gone; confirmed state is treated
        # as immutable during resume.
        assert not any(
            kind == "output" and "任务理解中" in str(payload.get("text"))
            for kind, payload in events
        )
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_pause_cancels_running_prepare_and_resume_restarts(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    calls = 0

    async def prepare_phase():
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()  # 一直等，直到被取消

    runtime = _prepare_runtime(tmp_path, prepare_phase)
    try:
        await runtime.start_task("pause during prepare")
        await asyncio.wait_for(started.wait(), timeout=1)
        assert runtime.session.lifecycle.task is not None
        assert not runtime.session.lifecycle.task.done()

        assert await runtime.message("/pause") == "WAITING"
        assert runtime.session.lifecycle.task.done()

        started.clear()
        assert await runtime.message("/resume") == "RUNNING"
        await asyncio.wait_for(started.wait(), timeout=1)
        assert calls == 2
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_stop_cancels_running_prepare(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def prepare_phase():
        started.set()
        await asyncio.Event().wait()

    runtime = _prepare_runtime(tmp_path, prepare_phase)
    try:
        await runtime.start_task("stop during prepare")
        await asyncio.wait_for(started.wait(), timeout=1)
        assert runtime.session.lifecycle.task is not None
        assert not runtime.session.lifecycle.task.done()

        assert await runtime.message("/stop") == "STOPPED"
        assert runtime.session.lifecycle.task.done()
    finally:
        await _close(runtime)

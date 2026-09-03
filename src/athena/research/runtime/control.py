"""Lifecycle and command handling for ``ResearchRuntime``."""

import asyncio
from typing import Any

from athena.research.runtime.resume_contract import (
    ResearchControlError,
    is_continue_command,
    resume_capability,
)


def _consume_lifecycle_result(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


def resume_task_text(runtime: Any, fallback: str) -> str:
    """Recover the original task instead of treating a resume command as work."""
    if runtime.state.task_text:
        return runtime.state.task_text
    understanding = runtime.state.task_understanding or {}
    parts = [
        str(understanding[key])
        for key in ("title", "dataset", "target")
        if understanding.get(key)
    ]
    return " ".join(parts) if parts else fallback


async def start(runtime: Any) -> asyncio.Task[None]:
    """Start infrastructure and return the single Supervisor lifecycle task."""
    lifecycle = runtime.session.lifecycle
    if lifecycle.task is not None and not lifecycle.task.done():
        return lifecycle.task

    lifecycle.task_text = resume_task_text(runtime, runtime.task_text)
    await runtime.git.init(initial_file=".gitignore", initial_content=".venv/\n")
    runtime.agents.start()
    runtime.start_survey()
    if runtime.state.status == "IDLE":
        runtime.state.status = "RUNNING"
        runtime.state.save(runtime.state_path)
    lifecycle.started = True
    lifecycle.task = asyncio.create_task(runtime.supervisor.start())
    lifecycle.task.add_done_callback(_consume_lifecycle_result)
    return lifecycle.task


def rearm_if_terminal(runtime: Any) -> None:
    """Clear a completed lifecycle task so the durable run can resume."""
    lifecycle = runtime.session.lifecycle
    if lifecycle.task is None or not lifecycle.task.done():
        return
    if runtime.state.status in {"FAILED", "STOPPED", "COMPLETED"}:
        lifecycle.task = None


async def start_task(runtime: Any, task: str) -> str:
    """Seed or resume a confirmed task and launch the phase machine."""
    runtime.session.lifecycle.task_text = resume_task_text(runtime, task)
    rearm_if_terminal(runtime)
    lifecycle = runtime.session.lifecycle
    if not lifecycle.started or lifecycle.task is None:
        if (
            runtime.tree.best_experiment_id() is None
            and runtime.state.phase != "PREPARE"
        ):
            runtime.state.phase = "PREPARE"
        await runtime.start()
    return runtime.state.status


async def start_validation(runtime: Any) -> str:
    """Enter VALIDATE from SEARCH or resume an interrupted validation."""
    if runtime.state.phase == "COMPLETED":
        return runtime.state.status
    await ensure_started(runtime)
    if runtime.state.phase == "SEARCH":
        await runtime.supervisor.set_phase_decision("VALIDATE")
    elif runtime.state.phase == "VALIDATE":
        await runtime.supervisor.continue_phase()
    else:
        raise ValueError(
            f"cannot VALIDATE from phase {runtime.state.phase}; run SEARCH first"
        )
    return runtime.state.status


async def message(runtime: Any, text: str) -> str:
    """Apply control commands or send ordinary text to Supervisor."""
    command_result = await _control_command(runtime, text.strip())
    if command_result is not None:
        return command_result
    if runtime.config.auto_seed_task and not runtime.session.lifecycle.started:
        return await start_task(runtime, text.strip())
    answer = await runtime.supervisor.message(text)
    if runtime.state.phase == "VALIDATE" and (
        runtime.session.lifecycle.task is None or runtime.session.lifecycle.task.done()
    ):
        await runtime.supervisor.continue_phase()
    return answer


async def _control_command(runtime: Any, command: str) -> str | None:
    if command == "/stop":
        async with runtime.session.lifecycle.resume_lock:
            status = await runtime.supervisor.request_stop()
            await cancel_supervisor_task(runtime)
            return status
    if command == "/pause":
        status = await runtime.supervisor.pause()
        if runtime.state.phase in {"PREPARE", "VALIDATE"}:
            await cancel_supervisor_task(runtime)
        return status
    if command == "/resume" or is_continue_command(command):
        return await runtime.resume_current_task()
    if command in {"/manual", "/auto"}:
        await ensure_started(runtime)
        manual = command == "/manual"
        await runtime.supervisor.set_manual_mode(manual)
        return f"manual mode {'on' if manual else 'off'}"
    if command.startswith("/select "):
        await ensure_started(runtime)
        hypothesis_id = command.removeprefix("/select ").strip()
        if not hypothesis_id:
            return "usage: /select <hypothesis_id>"
        await runtime.supervisor.select_next_hypothesis(hypothesis_id)
        return f"selected {hypothesis_id}"
    return None


async def resume_current_task(runtime: Any) -> str:
    """Resume the current durable task without changing its confirmed contract."""
    lifecycle = runtime.session.lifecycle
    async with lifecycle.resume_lock:
        original_status = runtime.state.status
        original_started = lifecycle.started
        capability = resume_capability(runtime.state)
        task = lifecycle.task
        live_task = task is not None and not task.done()

        if capability.reason == "already_running" and live_task:
            return runtime.state.status
        if not capability.available and capability.reason != "already_running":
            raise ResearchControlError(
                "resume_unavailable", "there is no interrupted task to continue"
            )

        lifecycle.task_text = resume_task_text(runtime, lifecycle.task_text)
        if runtime.state.status == "FAILED" and live_task:
            old_task = task
            await asyncio.gather(old_task, return_exceptions=True)
            if not old_task.done():
                raise RuntimeError("failed lifecycle task did not finish unwinding")

        task = lifecycle.task
        if task is None or task.done():
            rearm_if_terminal(runtime)
            try:
                await runtime.supervisor.resume(restarting=True)
                await runtime.start()
            except BaseException as error:
                task = lifecycle.task
                if task is None or task.done():
                    lifecycle.started = original_started
                    runtime.state.status = (
                        "FAILED" if original_status == "RUNNING" else original_status
                    )
                    try:
                        runtime.state.save(runtime.state_path)
                    except BaseException as rollback_error:
                        error.add_note(
                            f"resume rollback persistence failed: {rollback_error}"
                        )
                raise
        else:
            await runtime.supervisor.resume()
        return runtime.state.status


async def ensure_started(runtime: Any) -> None:
    """Start the Supervisor only when a trusted baseline already exists."""
    if (
        not runtime.session.lifecycle.started
        and runtime.tree.best_experiment_id() is not None
    ):
        await runtime.start()


async def cancel_supervisor_task(runtime: Any) -> None:
    """Cancel and join the current Supervisor lifecycle task."""
    task = runtime.session.lifecycle.task
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "cancel_supervisor_task",
    "ensure_started",
    "message",
    "rearm_if_terminal",
    "resume_current_task",
    "resume_task_text",
    "start",
    "start_task",
    "start_validation",
]

"""Lifecycle and control helpers for ``ResearchRuntime``.

Keeps start/stop/resume and task-understanding orchestration out of the
composition root. The runtime keeps thin delegating methods so its public
interface and existing tests remain unchanged.
"""

import asyncio
import logging
from typing import Any

from athena.research.runtime_events import recent_user_texts

logger = logging.getLogger(__name__)


def recent_user_texts_from(runtime: Any, limit: int = 6) -> list[str]:
    """Return recent Human messages from the persisted session transcript."""
    try:
        records = runtime.replay_output_events()
    except Exception:
        logger.warning("failed to replay session transcript", exc_info=True)
        return []
    return recent_user_texts(records, limit)


def task_context_text(task_text: str, prior: list[str]) -> str:
    """Build the supervisor task-understanding prompt from task + history."""
    if not prior:
        return task_text
    return (
        "Previous conversation:\n"
        + "\n".join(f"- {text}" for text in prior)
        + "\n\nCurrent task text:\n"
        + task_text
    )


def resume_task_text(runtime: Any, fallback: str) -> str:
    """Reconstruct the effective task text from persisted resume state.

    Prefer the first persisted full task text; fall back to a description
    built from structured task understanding so a bare "continue" is never fed
    to survey/PREPARE prompts.
    """
    if runtime.state.task_text:
        return runtime.state.task_text
    understanding = runtime.state.task_understanding or {}
    parts = [
        str(understanding[key])
        for key in ("title", "dataset", "target")
        if understanding.get(key)
    ]
    return " ".join(parts) if parts else fallback


async def maybe_run_task_understanding(runtime: Any) -> None:
    """Run PREPARE task understanding unless a checkpoint already exists.

    Failures only degrade to Kaggle tools staying off; they never block start.
    """
    if runtime._provider is None or runtime.state.phase != "PREPARE":
        return
    if runtime.state.task_understanding is not None:
        await runtime.publish_output(
            source="supervisor",
            channel="text",
            text="断点续传：复用已持久化的任务理解，跳过任务理解回合。",
        )
        return
    if not runtime._task_text.strip():
        return
    context = task_context_text(runtime._task_text, recent_user_texts_from(runtime))
    await runtime.publish_output(
        source="supervisor",
        channel="text",
        text="任务理解中：阅读任务并决定是否接入 Kaggle 工具…",
    )
    try:
        await runtime._agent_turns.run_supervisor_turn(context)
        await runtime.publish_output(
            source="supervisor", channel="text", text="任务理解完成。"
        )
    except Exception as error:
        logger.warning(
            "supervisor task-understanding turn failed; Kaggle tools stay off",
            exc_info=True,
        )
        await runtime.publish_output(
            source="supervisor",
            channel="error",
            text=f"任务理解失败（已降级继续）：{error}",
        )


async def start(runtime: Any) -> asyncio.Task[None]:
    """Start infrastructure and the single Supervisor loop once.

    Returns the Supervisor lifecycle task; callers may await it to block
    until PREPARE/SEARCH/VALIDATE reaches a terminal state.
    """
    if runtime._task is not None and not runtime._task.done():
        return runtime._task
    await runtime._git.init(initial_file=".gitignore", initial_content=".venv/\n")
    runtime._agents.start()
    # Run the survey in parallel with PREPARE: it is slow, and PREPARE does not
    # depend on it.
    runtime._start_survey()
    # Resume path: a direct start() also restores the first task text.
    runtime._task_text = resume_task_text(runtime, runtime._task_text)
    if runtime.state.status == "IDLE":
        runtime.state.status = "RUNNING"
        runtime.state.save(runtime._state_path)
    runtime._started = True

    async def _run_lifecycle() -> None:
        # Task understanding also lives in a cancellable background task so
        # start_search RPC does not block pause/stop, and the task is
        # cancellable before it is assigned to runtime._task.
        await maybe_run_task_understanding(runtime)
        await runtime._supervisor.start()

    runtime._task = asyncio.create_task(_run_lifecycle())
    return runtime._task


def rearm_if_terminal(runtime: Any) -> None:
    """Clear the done supervisor task so a terminal run can be restarted."""
    if runtime._started and runtime._task is not None and runtime._task.done():
        if runtime.state.status in {"FAILED", "STOPPED", "COMPLETED"}:
            runtime._task = None


async def start_task(runtime: Any, task: str) -> str:
    """Seed the research task and start PREPARE -> SEARCH -> VALIDATE.

    Fresh runs begin at PREPARE so a trusted baseline/SOTA is established
    before any SEARCH hypothesis can be proposed. Existing ``state.json``
    (resume) keeps its phase and starts via ``recover()``. A terminal run is
    re-armed in place; ``eda_dir`` and workspaces are left untouched.
    """
    if runtime.state.task_text is None and runtime.state.task_understanding is None:
        runtime.state.task_text = task
        runtime.state.save(runtime._state_path)
    runtime._task_text = resume_task_text(runtime, task)
    rearm_if_terminal(runtime)
    if not runtime._started or runtime._task is None:
        if (
            runtime.tree.best_experiment_id() is None
            and runtime.state.phase != "PREPARE"
        ):
            runtime.state.phase = "PREPARE"
        await runtime.start()
    return runtime.state.status


async def start_validation(runtime: Any) -> str:
    """Run the frozen-SOTA VALIDATE phase and publish the final report.

    Wires the GUI ``start_validation`` control to the supervisor phase
    machine. In the interactive path (``auto_validate=False``) SEARCH parks
    at ``WAITING`` after its budget; this transitions into VALIDATE and runs
    it. Idempotent once validation has already completed.
    """
    if runtime.state.phase == "COMPLETED":
        return runtime.state.status
    await ensure_started(runtime)
    phase = runtime.state.phase
    if phase == "SEARCH":
        await runtime._supervisor.set_phase_decision("VALIDATE")
    elif phase == "VALIDATE":
        await runtime._supervisor.continue_phase()
    else:
        raise ValueError(f"cannot VALIDATE from phase {phase}; run SEARCH first")
    return runtime.state.status


async def message(runtime: Any, text: str) -> str:
    """Apply exact control commands or delegate ordinary prose unchanged.

    With ``auto_seed_task`` (TUI entry), the first ordinary message before
    ``start()`` seeds the research task and starts PREPARE, so a trusted
    baseline/SOTA exists before SEARCH proposes hypotheses.
    """
    command = text.strip()
    if command == "/stop":
        status = await runtime._supervisor.request_stop()
        await cancel_supervisor_task(runtime)
        return status
    if command == "/pause":
        status = await runtime._supervisor.pause()
        # PREPARE/VALIDATE have no scheduler loop checkpoint; cancel the phase
        # task and let /resume re-enter the phase machine.
        if runtime.state.phase in {"PREPARE", "VALIDATE"}:
            await cancel_supervisor_task(runtime)
        return status
    if command == "/resume":
        if runtime._supervisor.is_stopped():
            return runtime.state.status
        if (runtime._task is None or runtime._task.done()) and runtime._started:
            await runtime._supervisor.resume(restarting=True)
            await runtime.start()
        else:
            await ensure_started(runtime)
            await runtime._supervisor.resume()
        return runtime.state.status
    if command == "/manual":
        await ensure_started(runtime)
        await runtime._supervisor.set_manual_mode(True)
        return "manual mode on"
    if command == "/auto":
        await ensure_started(runtime)
        await runtime._supervisor.set_manual_mode(False)
        return "manual mode off"
    if command.startswith("/select "):
        await ensure_started(runtime)
        hypothesis_id = command[len("/select ") :].strip()
        if not hypothesis_id:
            return "usage: /select <hypothesis_id>"
        await runtime._supervisor.select_next_hypothesis(hypothesis_id)
        return f"selected {hypothesis_id}"
    if runtime._auto_seed_task and not runtime._started:
        return await start_task(runtime, command)
    answer = await runtime._supervisor.message(text)
    # Interactive resume: a SupervisorAgent turn may transition an idle run
    # into VALIDATE. Re-enter the phase machine to actually execute it; the
    # live start() task (or auto_validate) handles the running case.
    if runtime.state.phase == "VALIDATE" and (
        runtime._task is None or runtime._task.done()
    ):
        await runtime._supervisor.continue_phase()
    return answer


async def ensure_started(runtime: Any) -> None:
    """Start (and recover) the Supervisor loop once a trusted baseline exists.

    No-op for a fresh project (no SOTA yet) so control commands never jump
    straight into SEARCH without PREPARE.
    """
    if not runtime._started and runtime.tree.best_experiment_id() is not None:
        await runtime.start()


async def cancel_supervisor_task(runtime: Any) -> None:
    """Cancel the current Supervisor lifecycle task (pause/stop interrupt)."""
    if runtime._task is not None and not runtime._task.done():
        runtime._task.cancel()
        await asyncio.gather(runtime._task, return_exceptions=True)

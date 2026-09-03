"""End-to-end policy tests for the task confirmation gate.

The full GUI RPC round-trip is owned by the RPC/context worker. These tests
verify the runtime-level compatibility matrix that Task 10 is responsible for:
explicit policies at every entry point, no implicit auto-confirm, and idempotent
confirmed-state recovery.
"""

import asyncio
import contextlib
from pathlib import Path

import pytest

from athena.research import ResearchRuntime
from athena.research.clarification.errors import ClarificationConfirmationError
from athena.research.clarification.models import ConfirmationJournal
from athena.research.clarification.persistence import ConfirmationJournalStore


async def _close(runtime: ResearchRuntime) -> None:
    task = getattr(runtime, "_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await task
    await runtime.aclose()


async def _prepare_noop() -> object:
    return object()


@pytest.mark.asyncio
async def test_default_runtime_requires_explicit_policy(tmp_path: Path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path, prepare_phase=_prepare_noop)
    try:
        with pytest.raises(
            ClarificationConfirmationError, match="confirmation_policy_required"
        ):
            await runtime.start_task("predict churn")
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_gated_runtime_rejects_raw_task_until_confirmed(
    tmp_path: Path,
) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path,
        prepare_phase=_prepare_noop,
        task_confirmation_gate=True,
        auto_confirm=False,
    )
    try:
        with pytest.raises(
            ClarificationConfirmationError, match="confirmation_required"
        ):
            await runtime.start_task("predict churn")
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_explicit_auto_confirm_starts_and_commits_draft(
    tmp_path: Path,
) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path,
        prepare_phase=_prepare_noop,
        task_confirmation_gate=False,
        auto_confirm=True,
    )
    try:
        status = await runtime.start_task("predict churn")
        assert status == "RUNNING"
        assert runtime.state.task_understanding is not None
        assert runtime.clarification_path.is_file()
        assert runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").is_file()
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_failed_lifecycle_task_can_be_started_again(tmp_path: Path) -> None:
    calls = 0
    restarted = asyncio.Event()

    async def prepare() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first PREPARE failed")
        restarted.set()
        await asyncio.Event().wait()

    runtime = ResearchRuntime(
        project_root=tmp_path,
        prepare_phase=prepare,
        task_confirmation_gate=False,
        auto_confirm=True,
    )
    try:
        await runtime.start_task("predict churn")
        first = runtime.session.lifecycle.task
        with pytest.raises(RuntimeError, match="first PREPARE failed"):
            await first
        assert runtime.state.status == "FAILED"

        before = {
            "task_text": runtime.state.task_text,
            "understanding": dict(runtime.state.task_understanding or {}),
            "handoff_refs": dict(runtime.state.handoff_refs),
            "draft": runtime.clarification_path.read_bytes(),
            "handoff": runtime.handoffs_path.joinpath(
                "TASK_CLARIFICATION.md"
            ).read_bytes(),
        }

        class ExplodingClarification:
            async def start_or_resume(self, _task: str) -> object:
                raise AssertionError("resume must not enter clarification")

        runtime.services.workflow.clarification = ExplodingClarification()

        assert await runtime.message("continue") == "RUNNING"
        await asyncio.wait_for(restarted.wait(), timeout=1)

        assert runtime.session.lifecycle.task is not first
        assert runtime.state.status == "RUNNING"
        assert calls == 2
        assert runtime.state.task_text == before["task_text"]
        assert runtime.state.task_understanding == before["understanding"]
        assert runtime.state.handoff_refs == before["handoff_refs"]
        assert runtime.clarification_path.read_bytes() == before["draft"]
        assert (
            runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").read_bytes()
            == before["handoff"]
        )
    finally:
        await _close(runtime)


@pytest.mark.asyncio
async def test_start_syncs_memory_after_confirmation_rollback(tmp_path: Path) -> None:
    async def prepare() -> object:
        await asyncio.Event().wait()

    runtime = ResearchRuntime(project_root=tmp_path, prepare_phase=prepare)
    try:
        runtime.state.task_text = "original task"
        runtime.state.task_understanding = {"title": "original"}
        runtime.state.handoff_refs = {}
        runtime.state.save(runtime.state_path)
        previous_state = runtime.state_path.read_text(encoding="utf-8")
        resume_path = runtime.state_path.with_name("resume.json")
        previous_resume = resume_path.read_text(encoding="utf-8")

        runtime.state.task_text = "partially committed task"
        runtime.state.task_understanding = {"title": "partial"}
        runtime.state.handoff_refs = {"task_clarification": "partial-ref"}
        runtime.state.save(runtime.state_path)
        journal = ConfirmationJournal(
            transaction_id="interrupted",
            session_id=runtime.session_id,
            draft_id="draft-1",
            revision=0,
            phase="PREPARED",
            previous_state_json=previous_state,
            previous_resume_json=previous_resume,
            previous_draft_json="{}",
            previous_handoff_text=None,
        )
        ConfirmationJournalStore(tmp_path / ".athena").save(journal)

        await runtime.start()

        assert runtime.state.task_text == "original task"
        assert runtime.state.task_understanding == {"title": "original"}
        assert runtime.state.handoff_refs == {}
    finally:
        await _close(runtime)

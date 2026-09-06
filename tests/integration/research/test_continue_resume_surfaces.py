"""Acceptance coverage for exact ``continue`` across Python-owned surfaces."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.gui.service import GuiService
from athena.research import ResearchRuntime
from athena.research.clarification.errors import ClarificationError
from athena.research.config import ResearchOptions, RuntimeDependencies, TaskConfig
from athena.research.runtime.clarification import auto_confirm

Phase = Literal["PREPARE", "SEARCH", "VALIDATE"]
EntryPoint = Literal[
    "runtime.message",
    "runtime.start_task",
    "gui.message",
    "gui.resume",
]


class _LocalBroker:
    async def ask(self, _request: object) -> object:
        raise AssertionError("the confirmed-draft conflict must not ask the broker")

    async def cancel_scope(self, _session_id: str, _scope_id: str) -> list[object]:
        return []


@dataclass
class PhaseFailureHarness:
    """A real runtime whose selected phase fails once and then blocks or fails."""

    runtime: ResearchRuntime
    phase: Phase
    first_task: asyncio.Task[None]
    second_entered: asyncio.Event
    release_second: asyncio.Event
    lifecycle_tasks: list[asyncio.Task[None]]
    output_errors: list[str]
    snapshot: dict[str, object]

    async def close(self) -> None:
        self.release_second.set()
        await self.runtime.aclose()


async def build_phase_failure_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: Phase,
    *,
    fail_second: bool = False,
) -> PhaseFailureHarness:
    """Create confirmed durable state without network or subprocess phase work."""
    runtime = ResearchRuntime(
        project_root=tmp_path,
        research=ResearchOptions(task=TaskConfig(auto_confirm=True)),
        dependencies=RuntimeDependencies(broker=_LocalBroker()),
    )

    async def fake_git_init(*_args: object, **_kwargs: object) -> str:
        return "test-commit"

    monkeypatch.setattr(runtime.git, "init", fake_git_init)
    confirmed = await auto_confirm(runtime, "original task")
    runtime.state.phase = phase
    runtime.state.status = "IDLE"
    runtime.state.save(runtime.state_path)

    second_entered = asyncio.Event()
    release_second = asyncio.Event()
    lifecycle_tasks: list[asyncio.Task[None]] = []
    output_errors: list[str] = []

    def capture(kind: str, data: dict[str, object]) -> None:
        if kind == "output" and data.get("channel") == "error":
            output_errors.append(str(data.get("text")))

    runtime.subscribe(capture)

    async def phase_entry(*_args: object, **_kwargs: object) -> None:
        current = asyncio.current_task()
        assert current is not None
        lifecycle_tasks.append(current)
        if len(lifecycle_tasks) == 1:
            raise RuntimeError(f"first {phase} failed")
        second_entered.set()
        if fail_second:
            raise RuntimeError(f"second {phase} failed")
        await release_second.wait()

    phase_machine = runtime.supervisor._phases
    if phase == "PREPARE":
        monkeypatch.setattr(phase_machine, "_run_prepare", phase_entry)
    elif phase == "SEARCH":
        monkeypatch.setattr(phase_machine._search, "run_search", phase_entry)
    else:
        monkeypatch.setattr(phase_machine, "_run_validation", phase_entry)

    first_task = await runtime.start()
    await asyncio.gather(first_task, return_exceptions=True)
    assert runtime.state.status == "FAILED"
    assert lifecycle_tasks == [first_task]

    draft_bytes = runtime.clarification_path.read_bytes()
    handoff_bytes = runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").read_bytes()
    draft_payload = json.loads(draft_bytes)
    return PhaseFailureHarness(
        runtime=runtime,
        phase=phase,
        first_task=first_task,
        second_entered=second_entered,
        release_second=release_second,
        lifecycle_tasks=lifecycle_tasks,
        output_errors=output_errors,
        snapshot={
            "task_text": runtime.state.task_text,
            "task_understanding": dict(runtime.state.task_understanding or {}),
            "handoff_refs": dict(runtime.state.handoff_refs),
            "draft_bytes": draft_bytes,
            "handoff_bytes": handoff_bytes,
            "draft_id": confirmed.draft_id,
            "revision": confirmed.revision,
            "persisted_draft_id": draft_payload["draft_id"],
            "persisted_revision": draft_payload["revision"],
        },
    )


async def resume_through(harness: PhaseFailureHarness, entry_point: EntryPoint) -> str:
    """Normalize the four supported caller return envelopes to one status."""
    runtime = harness.runtime
    service = GuiService(runtime)
    if entry_point == "runtime.message":
        return await runtime.message("continue")
    if entry_point == "runtime.start_task":
        return await runtime.start_task("continue")
    if entry_point == "gui.message":
        return str((await service.message("continue"))["response"])
    return str((await service.resume())["status"])


def assert_confirmed_contract_unchanged(harness: PhaseFailureHarness) -> None:
    """Assert every confirmation-owned identity and artifact remains frozen."""
    runtime = harness.runtime
    snapshot = harness.snapshot
    draft_payload = json.loads(runtime.clarification_path.read_bytes())
    assert runtime.state.task_text == snapshot["task_text"] == "original task"
    assert runtime.state.task_understanding == snapshot["task_understanding"]
    assert runtime.state.handoff_refs == snapshot["handoff_refs"]
    assert runtime.clarification_path.read_bytes() == snapshot["draft_bytes"]
    assert (
        runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").read_bytes()
        == snapshot["handoff_bytes"]
    )
    assert draft_payload["draft_id"] == snapshot["draft_id"]
    assert draft_payload["revision"] == snapshot["revision"]
    assert draft_payload["draft_id"] == snapshot["persisted_draft_id"]
    assert draft_payload["revision"] == snapshot["persisted_revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["PREPARE", "SEARCH", "VALIDATE"])
@pytest.mark.parametrize(
    "entry_point",
    ["runtime.message", "runtime.start_task", "gui.message", "gui.resume"],
)
async def test_continue_restarts_the_same_confirmed_phase_across_python_surfaces(
    phase: Phase,
    entry_point: EntryPoint,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await build_phase_failure_harness(tmp_path, monkeypatch, phase)
    try:
        assert await resume_through(harness, entry_point) == "RUNNING"
        await asyncio.wait_for(harness.second_entered.wait(), timeout=1)

        second_task = harness.runtime.session.lifecycle.task
        assert second_task is not None
        assert second_task is not harness.first_task
        assert harness.lifecycle_tasks == [harness.first_task, second_task]
        assert harness.runtime.state.phase == phase
        assert harness.runtime.state.status == "RUNNING"
        assert_confirmed_contract_unchanged(harness)
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["PREPARE", "SEARCH", "VALIDATE"])
async def test_second_phase_failure_is_surfaced_without_a_third_lifecycle(
    phase: Phase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await build_phase_failure_harness(
        tmp_path, monkeypatch, phase, fail_second=True
    )
    try:
        assert await resume_through(harness, "runtime.message") == "RUNNING"
        await asyncio.wait_for(harness.second_entered.wait(), timeout=1)
        second_task = harness.runtime.session.lifecycle.task
        assert second_task is not None
        await asyncio.gather(second_task, return_exceptions=True)

        assert harness.runtime.state.status == "FAILED"
        assert harness.runtime.state.phase == phase
        assert harness.lifecycle_tasks == [harness.first_task, second_task]
        assert harness.runtime.session.lifecycle.task is second_task
        assert any(f"second {phase} failed" in error for error in harness.output_errors)
        assert_confirmed_contract_unchanged(harness)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_genuinely_new_gui_task_keeps_different_task_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await build_phase_failure_harness(tmp_path, monkeypatch, "PREPARE")
    try:
        service = GuiService(harness.runtime)
        with pytest.raises(ClarificationError) as caught:
            await service.task_clarification_start("genuinely different task")

        assert caught.value.code == "different_task"
        assert_confirmed_contract_unchanged(harness)
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["SEARCH", "VALIDATE"])
async def test_saved_tree_recovery_keeps_runtime_and_supervisor_identity(
    phase: Phase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await build_phase_failure_harness(tmp_path, monkeypatch, phase)
    try:
        runtime = harness.runtime
        saved_tree = ResearchTree()
        saved_tree.add_hypothesis(
            Hypothesis(
                id="baseline",
                statement="saved baseline",
                intervention="restore saved history",
                expected_effect="preserve the trusted history",
            )
        )
        saved_tree.add_experiment(
            "exp_baseline",
            Experiment(
                hypothesis_id="baseline",
                commit="a" * 40,
                plan=ExperimentPlan(
                    kind="baseline",
                    change="restore saved baseline",
                    run_config_ref="artifact://baseline/config",
                    budget={},
                    acceptance_rule="saved history remains available",
                ),
                gitwork=GitWorkBranch(
                    path=str(tmp_path), branch="main", base_commit="a" * 40
                ),
                status=ExperimentStatus.SUCCEEDED,
                eval=EvalResult(
                    experiment_id="exp_baseline",
                    primary=0.8,
                    per_sample="artifact://baseline/samples",
                ),
            ),
        )
        saved_tree.set_sota("exp_baseline")
        saved_tree.save(runtime.tree_path)
        runtime.state.phase = phase
        runtime.state.status = "IDLE"
        runtime.state.save(runtime.state_path)

        shared_tree = runtime.tree
        await runtime.supervisor.recover()

        assert runtime.tree is shared_tree
        assert runtime.tree is runtime.supervisor.tree
        service = GuiService(runtime)
        assert service.tree_get()["tree"] == saved_tree.to_dict()

        runtime.supervisor.tree.update_hypothesis_status("baseline", "SUPPORTED")
        assert (
            service.tree_get()["tree"]["hypotheses"]["baseline"]["status"]
            == "SUPPORTED"
        )
        runtime.save_tree()
        assert ResearchTree.load(runtime.tree_path).to_dict() == runtime.tree.to_dict()
    finally:
        await harness.close()

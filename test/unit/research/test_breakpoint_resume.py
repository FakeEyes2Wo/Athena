"""Hermetic tests for the breakpoint-resume behavior added in this plan.

These tests stub out git/agent subprocesses so they run inside the sandbox;
the sandbox denies named pipes used by real ``git`` subprocess capture.
"""

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.phase_runner import PhaseRunner
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.prepare import PrepareResult


def _stub_runtime(tmp_path: Path, *, task_understanding=None) -> ResearchRuntime:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._root = tmp_path
    runtime._state_path = tmp_path / ".athena" / "state.json"
    runtime._provider = object()
    runtime._task_text = "predict titanic survival"
    runtime._started = False
    runtime._task = None
    runtime._survey_enabled = False
    runtime._survey_task = None
    runtime._state = SimpleNamespace(
        phase="PREPARE",
        status="RUNNING",
        task_understanding=task_understanding,
        task_text="predict titanic survival" if task_understanding else None,
        save=lambda path: None,
    )
    runtime._supervisor = SimpleNamespace(state=runtime._state)

    class FakeGit:
        async def init(self, *args, **kwargs):
            return None

    class FakeAgents:
        def start(self):
            return None

    runtime._git = FakeGit()
    runtime._agents = FakeAgents()
    return runtime


@pytest.mark.asyncio
async def test_start_task_persists_first_task_text_and_reuses_it(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    saves: list[str] = []

    def save(path) -> None:
        saves.append(str(path))

    runtime._state.save = save
    runtime._started = True
    runtime._task = SimpleNamespace(done=lambda: False)

    status = await runtime.start_task("predict titanic survival")

    assert status == "RUNNING"
    assert runtime._task_text == "predict titanic survival"
    assert runtime._state.task_text == "predict titanic survival"
    assert saves == [str(runtime._state_path)]

    saves.clear()
    runtime._state.task_understanding = {"title": "titanic"}
    await runtime.start_task("continue")

    assert runtime._task_text == "predict titanic survival"
    assert runtime._state.task_text == "predict titanic survival"
    assert saves == []


@pytest.mark.asyncio
async def test_start_skips_task_understanding_when_persisted(tmp_path: Path) -> None:
    runtime = _stub_runtime(
        tmp_path, task_understanding={"title": "titanic", "target": "survival"}
    )
    outputs: list[dict[str, object]] = []

    async def publish_output(**kwargs) -> None:
        outputs.append(kwargs)

    runtime.publish_output = publish_output  # type: ignore[method-assign]
    runtime._start_survey = lambda: None  # type: ignore[method-assign]

    class FakeAgentTurns:
        async def run_supervisor_turn(self, text):
            raise AssertionError("task understanding must be skipped")

    runtime._agent_turns = FakeAgentTurns()

    class FakeSupervisor:
        def __init__(self, state) -> None:
            self.state = state

        async def start(self):
            return None

    runtime._supervisor = FakeSupervisor(runtime._state)

    await runtime.start()

    assert runtime._started is True
    assert runtime._task is not None
    assert any(
        "断点续传：复用已持久化的任务理解" in str(output.get("text"))
        for output in outputs
    )
    assert not any("任务理解中" in str(output.get("text")) for output in outputs)
    runtime._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runtime._task


@pytest.mark.asyncio
async def test_run_prepare_phase_reuses_frozen_evaluator(
    tmp_path: Path, monkeypatch
) -> None:
    from athena.research.phase_runner import PhaseRunner

    class FakeSupervisor:
        def __init__(self, frozen_ref: str) -> None:
            self._evaluator_ref = frozen_ref
            self.checked_refs: list[str] = []

        @property
        def evaluator_ref(self) -> str:
            return self._evaluator_ref

        async def checkpoint_evaluator(self, ref: str) -> None:
            self.checked_refs.append(ref)

    class FakeStore:
        async def get_text(self, ref):
            return '{"frozen": true}'

        async def put_text(self, text):
            return "tree-ref"

    class FakeBus:
        def project_agent_event(self, *args, **kwargs):
            return None

    class FakeGit:
        async def init(self, *args, **kwargs):
            return "base-commit"

        async def create(self, commit, branch, name=None):
            return SimpleNamespace(
                path=str(tmp_path / "workspaces" / "eda"),
                base_commit=commit,
            )

    outputs: list[dict[str, object]] = []

    async def publish_output(**kwargs) -> None:
        outputs.append(kwargs)

    frozen_ref = "sha256:" + "f" * 64
    state = SimpleNamespace(
        phase="PREPARE", status="RUNNING", eda_dir=None, save=lambda path: None
    )
    rt = SimpleNamespace(
        _root=tmp_path,
        _state_path=tmp_path / ".athena" / "state.json",
        _workspaces_root=tmp_path / "workspaces",
        _prepare_phase=None,
        _provider=object(),
        _task_text="predict survival",
        _agents=object(),
        _evaluator=object(),
        _execution=object(),
        _supervisor=FakeSupervisor(frozen_ref),
        _state=state,
        _store=FakeStore(),
        _git=FakeGit(),
        _registry=SimpleNamespace(contains=lambda name: True),
        _events_bus=FakeBus(),
        tree=SimpleNamespace(to_dict=lambda: {}),
        publish_output=publish_output,
    )

    async def run_evaluator_plan(**kwargs):
        del kwargs
        raise AssertionError("evaluator must be skipped when a checkpoint exists")

    async def run_prepare_plan(**kwargs):
        return PrepareResult(
            evaluator_ref=kwargs["evaluator_ref"],
            metric=0.71,
            commit=kwargs["workspace"].base_commit,
            predictions_ref="pred-ref",
            evidence_ref="evidence-ref",
            report_ref="report-ref",
        )

    monkeypatch.setattr(
        "athena.research.phase_runner.run_evaluator_plan",
        run_evaluator_plan,
        raising=False,
    )
    monkeypatch.setattr(
        "athena.research.phase_runner.run_prepare_plan", run_prepare_plan, raising=False
    )

    result = await PhaseRunner(rt).run_prepare_phase()

    assert result.evaluator_ref == frozen_ref
    assert any(
        "复用已冻结的评估器断点" in str(output.get("text")) for output in outputs
    )
    assert rt._supervisor.checked_refs == []


@pytest.mark.asyncio
async def test_run_general_turn_persists_agent_id_before_wait(
    tmp_path: Path, monkeypatch
) -> None:
    from athena.agents.general_agent import GeneralResult
    from athena.research import agent_turn_runner as atr

    saves: list[str] = []
    state = SimpleNamespace(
        phase="PREPARE",
        status="RUNNING",
        task_research_ref=None,
        task_research_agent_id=None,
        save=lambda path: saves.append(str(path)),
    )

    class FakeAgents:
        async def create_root(self, agent_type, request, *, name, agent_id=None):
            del name, agent_id
            return "general-worker", "run-1"

    rt = SimpleNamespace(
        _root=tmp_path,
        _state_path=tmp_path / ".athena" / "state.json",
        _state=state,
        _provider=object(),
        _registry=SimpleNamespace(contains=lambda name: True),
        _store=object(),
        _execution=object(),
        _agents=FakeAgents(),
        _events_bus=SimpleNamespace(project_agent_event=lambda *a, **k: None),
    )

    async def wait_run_events(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace()

    monkeypatch.setattr(atr, "wait_run_events", wait_run_events, raising=False)

    async def load_agent_result(summary, store, schema):
        del summary, store, schema
        return GeneralResult(result="done", files=["summary.md"])

    monkeypatch.setattr(atr, "load_agent_result", load_agent_result, raising=False)

    outcome = await atr.AgentTurnRunner(rt).run_general_turn("inspect competition")

    assert outcome.agent_id == "general-worker"
    assert outcome.result == {"result": "done", "files": ["summary.md"]}
    assert state.task_research_agent_id == "general-worker"
    assert saves == [str(rt._state_path)]

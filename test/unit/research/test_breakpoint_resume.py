"""Hermetic tests for the breakpoint-resume behavior added in this plan.

These tests stub out git/agent subprocesses so they run inside the sandbox;
the sandbox denies named pipes used by real ``git`` subprocess capture.
"""

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.phase_runner import PhaseRunner
from athena.research.rubrics.models import EvaluationPolicy
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
        evaluation_policy_ref=None,
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


class _PolicySupervisor:
    def __init__(self, state) -> None:
        self.state = state
        self.evaluation_policy = None

    def apply_evaluation_policy(self, policy: EvaluationPolicy) -> None:
        self.evaluation_policy = policy


@pytest.mark.asyncio
async def test_search_resume_restores_exact_frozen_evaluation_policy(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime._state.phase = "SEARCH"
    runtime._store = LocalArtifactStore(tmp_path / "artifacts")
    policy = EvaluationPolicy(
        primary_metric="roc_auc",
        direction="maximize",
        metric_source="human",
        locked=True,
        confidence=1.0,
        explanation="Explicit human objective.",
    )
    ref = await runtime._store.put_text(policy.model_dump_json())
    runtime._state.evaluation_policy_ref = ref
    runtime._supervisor = _PolicySupervisor(runtime._state)
    outputs: list[dict[str, object]] = []

    async def publish_output(**kwargs) -> None:
        outputs.append(kwargs)

    runtime.publish_output = publish_output  # type: ignore[method-assign]

    class FakeAgentTurns:
        async def run_evaluation_rubric(self):
            raise AssertionError("resume must not call the Evaluation Rubric LLM")

    runtime._agent_turns = FakeAgentTurns()

    await runtime._ensure_evaluation_policy()

    assert runtime._supervisor.evaluation_policy == policy
    assert runtime._direction == "maximize"
    assert runtime._state.evaluation_policy_ref == ref
    assert any("复用已冻结的评价策略" in str(item.get("text")) for item in outputs)


@pytest.mark.asyncio
async def test_invalid_frozen_policy_is_not_regenerated(tmp_path: Path) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime._state.phase = "PREPARE"
    runtime._state.evaluation_policy_ref = "sha256:" + "f" * 64
    runtime._store = LocalArtifactStore(tmp_path / "artifacts")
    runtime._supervisor = _PolicySupervisor(runtime._state)
    calls = 0

    class FakeAgentTurns:
        async def run_evaluation_rubric(self):
            nonlocal calls
            calls += 1

    runtime._agent_turns = FakeAgentTurns()

    with pytest.raises(RuntimeError, match="refusing to regenerate"):
        await runtime._ensure_evaluation_policy()

    assert calls == 0


@pytest.mark.asyncio
async def test_search_without_frozen_policy_fails_closed(tmp_path: Path) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime._state.phase = "SEARCH"
    runtime._store = LocalArtifactStore(tmp_path / "artifacts")
    runtime._supervisor = _PolicySupervisor(runtime._state)

    class FakeAgentTurns:
        async def run_evaluation_rubric(self):
            raise AssertionError("SEARCH must not generate a replacement policy")

    runtime._agent_turns = FakeAgentTurns()

    with pytest.raises(RuntimeError, match="missing outside PREPARE"):
        await runtime._ensure_evaluation_policy()


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
@pytest.mark.parametrize("terminal_status", ["FAILED", "STOPPED"])
async def test_start_task_rearms_persisted_search_terminal_state(
    tmp_path: Path, terminal_status: str
) -> None:
    """The direct headless start path must re-arm a persisted terminal SEARCH."""

    runtime = _stub_runtime(tmp_path, task_understanding={"title": "support2"})
    runtime._state.phase = "SEARCH"
    runtime._state.status = terminal_status
    saves: list[tuple[str, str]] = []
    started_with: list[str] = []

    def save(path) -> None:
        saves.append((str(path), runtime._state.status))

    async def supervisor_start() -> None:
        started_with.append(runtime._state.status)

    async def ready() -> bool:
        return True

    async def no_op() -> None:
        return None

    runtime._state.save = save
    runtime._supervisor.start = supervisor_start
    runtime._maybe_run_task_understanding = ready
    runtime._ensure_evaluation_policy = no_op
    runtime._start_survey = lambda: None

    task = await runtime.start()
    await task

    assert runtime._state.status == "RUNNING"
    assert started_with == ["RUNNING"]
    assert saves == [(str(runtime._state_path), "RUNNING")]


@pytest.mark.asyncio
async def test_start_task_keeps_task_text_when_understanding_turn_crashed(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    saves: list[str] = []

    def save(path) -> None:
        saves.append(str(path))

    runtime._state.save = save
    runtime._state.task_text = "https://www.kaggle.com/competitions/kaggriculture"
    runtime._state.task_understanding = None
    runtime._started = True
    runtime._task = SimpleNamespace(done=lambda: False)

    status = await runtime.start_task("continue")

    assert status == "RUNNING"
    assert runtime._task_text == "https://www.kaggle.com/competitions/kaggriculture"
    assert (
        runtime._state.task_text == "https://www.kaggle.com/competitions/kaggriculture"
    )
    assert saves == []


@pytest.mark.asyncio
async def test_start_skips_task_understanding_when_persisted(tmp_path: Path) -> None:
    runtime = _stub_runtime(
        tmp_path,
        task_understanding={
            "title": "titanic",
            "dataset": "Titanic passenger table",
            "target": "survival",
            "task_type": "classification",
            "metric_source": "unresolved",
            "readiness": "READY",
        },
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
    for _ in range(50):
        if any(
            "断点续传：复用已持久化的任务理解" in str(output.get("text"))
            for output in outputs
        ):
            break
        await asyncio.sleep(0.01)
    assert any(
        "断点续传：复用已验证 READY 的任务理解" in str(output.get("text"))
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
        def __init__(self, frozen_ref: str, final_ref: str) -> None:
            self._evaluator_ref = frozen_ref
            self._final_evaluator_ref = final_ref
            self.evaluation_policy = object()
            self.checked_refs: list[str] = []
            self.checked_final_refs: list[str] = []

        @property
        def evaluator_ref(self) -> str:
            return self._evaluator_ref

        @property
        def final_evaluator_ref(self) -> str:
            return self._final_evaluator_ref

        async def checkpoint_evaluator(self, ref: str) -> None:
            self.checked_refs.append(ref)

        async def checkpoint_final_evaluator(self, ref: str) -> None:
            self.checked_final_refs.append(ref)

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
    final_ref = "sha256:" + "e" * 64
    state = SimpleNamespace(
        phase="PREPARE", status="RUNNING", eda_dir=None, save=lambda path: None
    )
    supervisor = FakeSupervisor(frozen_ref, final_ref)
    rt = SimpleNamespace(
        _root=tmp_path,
        _state_path=tmp_path / ".athena" / "state.json",
        _workspaces_root=tmp_path / "workspaces",
        _prepare_phase=None,
        prepare_phase=None,
        _provider=object(),
        provider=object(),
        _task_text="predict survival",
        _agents=SimpleNamespace(reap=lambda agent_id: None),
        _evaluator=object(),
        _execution=object(),
        _state=state,
        state=state,
        _supervisor=supervisor,
        supervisor=supervisor,
        _store=FakeStore(),
        _git=FakeGit(),
        _registry=SimpleNamespace(contains=lambda name: True),
        _events_bus=FakeBus(),
        tree=SimpleNamespace(to_dict=lambda: {}),
        publish_output=publish_output,
        root=tmp_path,
        state_path=tmp_path / ".athena" / "state.json",
        workspaces_root=tmp_path / "workspaces",
        config=SimpleNamespace(dataset_path=None, target_column=None, split_seed=0),
        git=FakeGit(),
        store=FakeStore(),
        events=FakeBus(),
        registry=SimpleNamespace(contains=lambda name: True),
        agents=SimpleNamespace(reap=lambda agent_id: None),
        execution=object(),
        evaluator=object(),
        task_text="predict survival",
        kaggle_tools=lambda agent_type: None,
        ideator_tools=lambda: None,
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
        "athena.research.prepare_phase.run_evaluator_plan",
        run_evaluator_plan,
        raising=False,
    )
    monkeypatch.setattr(
        "athena.research.prepare_phase.run_prepare_plan",
        run_prepare_plan,
        raising=False,
    )

    async def fake_handoff(
        self, agent_id, agent_type, workspace, output_file, content, *, reap_after=False
    ):
        del agent_id, agent_type, content, reap_after
        Path(workspace).mkdir(parents=True, exist_ok=True)
        (Path(workspace) / output_file).write_text("stub\n", encoding="utf-8")

    monkeypatch.setattr(PhaseRunner, "_run_handoff_agent", fake_handoff)

    async def fake_eda_todos(*args, **kwargs):
        return []

    monkeypatch.setattr("athena.research.prepare_phase.run_eda_todos", fake_eda_todos)

    result = await PhaseRunner(rt).run_prepare_phase()

    assert result.evaluator_ref == frozen_ref
    assert result.final_evaluator_ref == final_ref
    assert any(
        "复用已冻结的评估器断点" in str(output.get("text")) for output in outputs
    )
    assert rt._supervisor.checked_refs == []


@pytest.mark.asyncio
async def test_run_general_turn_persists_agent_id_before_wait(
    tmp_path: Path, monkeypatch
) -> None:
    from athena.agents.task_agents import GeneralResult
    from athena.research import agent_turn_runner as atr

    saves: list[str] = []
    state = SimpleNamespace(
        phase="PREPARE",
        status="RUNNING",
        task_research_task=None,
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
        state=state,
        _provider=object(),
        provider=object(),
        _registry=SimpleNamespace(contains=lambda name: True),
        registry=SimpleNamespace(contains=lambda name: True),
        _store=object(),
        store=object(),
        _execution=object(),
        execution=object(),
        _agents=FakeAgents(),
        agents=FakeAgents(),
        _events_bus=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        events=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        root=tmp_path,
        state_path=tmp_path / ".athena" / "state.json",
        kaggle_tools=lambda kind: None,
    )

    async def wait_run_events(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace()

    monkeypatch.setattr(
        "athena.research.agent_turn_common.wait_run_events",
        wait_run_events,
        raising=False,
    )

    async def load_agent_result(summary, store, schema):
        del summary, store, schema
        return GeneralResult(result="done", files=["summary.md"])

    monkeypatch.setattr(
        "athena.research.agent_turn_general.load_agent_result",
        load_agent_result,
        raising=False,
    )

    outcome = await atr.AgentTurnRunner(rt).run_general_turn("inspect competition")

    assert outcome.agent_id == "general-worker"
    assert outcome.result == {"result": "done", "files": ["summary.md"]}
    assert state.task_research_task == "inspect competition"
    assert state.task_research_agent_id == "general-worker"
    assert saves == [str(rt._state_path)]


@pytest.mark.asyncio
async def test_run_general_turn_interrupts_worker_on_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    from athena.research import agent_turn_runner as atr

    state = SimpleNamespace(
        phase="PREPARE",
        status="RUNNING",
        task_research_task=None,
        task_research_ref=None,
        task_research_agent_id=None,
        save=lambda path: None,
    )
    interrupted: list[tuple[str, str]] = []

    class FakeAgents:
        async def create_root(self, agent_type, request, *, name, agent_id=None):
            del name, agent_id
            return "general-worker", "run-1"

        async def interrupt(self, agent_id, reason):
            interrupted.append((agent_id, reason))

    rt = SimpleNamespace(
        _root=tmp_path,
        _state_path=tmp_path / ".athena" / "state.json",
        _state=state,
        state=state,
        _provider=object(),
        provider=object(),
        _registry=SimpleNamespace(contains=lambda name: True),
        registry=SimpleNamespace(contains=lambda name: True),
        _store=object(),
        store=object(),
        _execution=object(),
        execution=object(),
        _agents=FakeAgents(),
        agents=FakeAgents(),
        _events_bus=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        events=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        root=tmp_path,
        state_path=tmp_path / ".athena" / "state.json",
        kaggle_tools=lambda kind: None,
    )
    monkeypatch.setattr(
        "athena.research.agent_turn_common.AGENT_TURN_TIMEOUT_SECONDS", 0
    )

    async def never_finishes(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(10)

    monkeypatch.setattr(
        "athena.research.agent_turn_common.wait_run_events",
        never_finishes,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        await atr.AgentTurnRunner(rt).run_general_turn("inspect competition")

    assert interrupted == [("general-worker", "general_turn_timeout")]


@pytest.mark.asyncio
async def test_start_task_reconstructs_task_text_from_legacy_understanding(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime._state.task_text = None
    runtime._state.task_understanding = {
        "title": "Kaggriculture farming simulation",
        "dataset": "kaggriculture environment",
        "target": "maximize income",
    }
    runtime._started = True
    runtime._task = SimpleNamespace(done=lambda: False)

    status = await runtime.start_task("continue")

    assert status == "RUNNING"
    assert runtime._task_text == (
        "Kaggriculture farming simulation kaggriculture environment maximize income"
    )
    assert runtime._state.task_text is None

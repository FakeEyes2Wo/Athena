"""Single-writer Supervisor Plan creation contracts."""

import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents.plan_agent import register_plan_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.execution.runtime import ExecutionRuntime
from athena.research.contracts import GeneralTurnOutcome
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor, _final_report_text


class _SubmitProvider:
    model_name = "plan-submit-test"

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        payload = json.dumps({"decision": "submit", "reason": "ready"})
        yield StreamEvent(
            kind="text_delta", data={"delta": payload, "accumulated": payload}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


@pytest.mark.asyncio
async def test_new_plan_freezes_evaluator_tree_and_human_context(
    tmp_path: Path, monkeypatch
) -> None:
    try:
        Supervisor = import_module("athena.research.supervisor.supervisor").Supervisor
    except ModuleNotFoundError:
        pytest.fail("Supervisor start_plan is missing")

    for name, value in {
        "GIT_AUTHOR_NAME": "Athena Test",
        "GIT_AUTHOR_EMAIL": "athena@example.invalid",
        "GIT_COMMITTER_NAME": "Athena Test",
        "GIT_COMMITTER_EMAIL": "athena@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)

    store = LocalArtifactStore(tmp_path / "artifacts")
    workspaces = LocalGitWorkspace(
        tmp_path / "repo", tmp_path / "worktrees", store.put_bytes
    )
    base_commit = await workspaces.init()
    evaluator_ref = await store.put_text('{"frozen":true}')
    evidence_ref = await store.put_text("baseline evidence")
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="baseline",
            statement="baseline",
            intervention="fit baseline",
            expected_effect="establish reference",
        )
    )
    tree.add_experiment(
        "exp_baseline",
        Experiment(
            hypothesis_id="baseline",
            commit=base_commit,
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref=evaluator_ref,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path=str(tmp_path / "repo"), branch="main", base_commit=base_commit
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline",
                primary=0.8,
                per_sample=evidence_ref,
            ),
        ),
    )
    tree.set_sota("exp_baseline")
    tree.add_hypothesis(
        Hypothesis(
            id="hyp_vit",
            parent_id="exp_baseline",
            statement="attention helps",
            intervention="use ViT",
            expected_effect="improve score",
            priority=1000.0,
            turn_limit=4,
            patience=2,
        )
    )
    state = ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=10,
        concurrency=4,
    )
    registry = AgentTypeRegistry()
    supervisor_holder: dict[str, object] = {}
    execution = ExecutionRuntime(project_root=tmp_path, store=store)
    register_plan_agent(
        registry,
        provider=_SubmitProvider(),
        artifacts=store,
        workspace_for=lambda plan_id: supervisor_holder["supervisor"].workspace_path(
            plan_id
        ),
        execution=execution,
    )
    agents = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / ".athena" / "logs" / "agents",
    )
    agents.start()

    async def unused_plan_turn(_plan_id, _state):
        raise AssertionError("Plan turn must not run during start_plan")

    async def unused_supervisor_turn(_text):
        raise AssertionError("SupervisorAgent turn is not used in this test")

    async def publish(_kind, _payload):
        return None

    supervisor = Supervisor(
        project_root=tmp_path,
        state=state,
        tree=tree,
        store=store,
        agents=agents,
        workspaces=workspaces,
        scheduler=Scheduler(),
        recovery=Recovery(),
        evaluator_ref=evaluator_ref,
        run_plan_turn=unused_plan_turn,
        run_supervisor_turn=unused_supervisor_turn,
        publish=publish,
    )
    supervisor_holder["supervisor"] = supervisor
    await supervisor.record_guidance("use ViT next", scope="next")
    await supervisor.start_plan("hyp_vit")

    frozen = await supervisor.plan_input("hyp_vit")
    assert frozen.hypothesis is not None
    assert frozen.hypothesis.id == "hyp_vit"
    assert frozen.evaluator_ref == evaluator_ref
    assert frozen.human_context == "use ViT next"
    assert supervisor.plan_identity("hyp_vit") == {
        "plan": "hyp_vit",
        "agent": "hyp_vit",
        "workspace": "hyp_vit",
        "log": "hyp_vit",
    }
    assert agents.has_agent("hyp_vit")
    persisted = ResearchState.load(tmp_path / ".athena" / "state.json")
    assert persisted.plans["hyp_vit"].context_ref == state.plans["hyp_vit"].context_ref
    assert persisted.plans["hyp_vit"].turns_used == 0
    persisted_tree = ResearchTree.load(tmp_path / ".athena" / "research_tree.json")
    assert persisted_tree.experiment_for_hypothesis("hyp_vit") == "exp_hyp_vit"
    assert (
        persisted_tree.get_experiment("exp_hyp_vit").status
        is ExperimentStatus.RUNNING
    )
    await supervisor.stop()
    await agents.aclose()


def test_final_report_text_surfaces_validation_metrics() -> None:
    text = _final_report_text(
        {
            "final_test_score": 0.81234,
            "generalization_gap": 0.0257,
            "generalization_warning": True,
        }
    )

    assert text.startswith("VALIDATE completed")
    assert "final test score 0.8123" in text
    assert "generalization gap 0.0257" in text
    assert "generalization warning" in text


def test_final_report_text_is_minimal_without_metrics() -> None:
    assert _final_report_text({}) == "VALIDATE completed"


def _checkpoint_supervisor(tmp_path: Path, run_general_turn=None) -> Supervisor:
    """Build a lightweight Supervisor for checkpoint-related unit tests."""
    state_path = tmp_path / ".athena" / "state.json"
    state = (
        ResearchState.load(state_path)
        if state_path.is_file()
        else ResearchState(
            status="RUNNING", phase="PREPARE", search_limit=10, concurrency=1
        )
    )

    async def no_plan_turn(_plan_id, _state):
        raise AssertionError("plan turn must not run")

    async def no_supervisor_turn(_text):
        raise AssertionError("supervisor turn must not run")

    async def publish(_kind, _payload):
        return None

    return Supervisor(
        project_root=tmp_path,
        state=state,
        tree=ResearchTree(),
        store=LocalArtifactStore(tmp_path / "artifacts"),
        agents=SimpleNamespace(),
        workspaces=SimpleNamespace(),
        scheduler=Scheduler(),
        recovery=Recovery(),
        evaluator_ref=None,
        run_plan_turn=no_plan_turn,
        run_supervisor_turn=no_supervisor_turn,
        publish=publish,
        run_general_turn=run_general_turn,
    )


@pytest.mark.asyncio
async def test_dispatch_general_caches_first_result(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []

    async def fake_general(task: str, prior_agent_id: str | None):
        calls.append((task, prior_agent_id))
        if task == "submit final predictions":
            return GeneralTurnOutcome(
                agent_id="general-submit",
                result={"result": "submitted", "files": []},
            )
        return GeneralTurnOutcome(
            agent_id="general-worker",
            result={"result": "done", "files": ["summary.md"]},
        )

    supervisor = _checkpoint_supervisor(tmp_path, fake_general)

    first = await supervisor.dispatch_general("inspect competition")
    second = await supervisor.dispatch_general("inspect competition")
    different = await supervisor.dispatch_general("submit final predictions")

    assert first == {"result": "done", "files": ["summary.md"]}
    assert second == {"cached": True, "result": "done", "files": ["summary.md"]}
    assert different == {"result": "submitted", "files": []}
    assert calls == [
        ("inspect competition", None),
        ("submit final predictions", None),
    ]
    assert supervisor.state.task_research_task == "inspect competition"
    assert supervisor.state.task_research_ref is not None
    assert supervisor.state.task_research_agent_id == "general-worker"
    persisted = ResearchState.load(tmp_path / ".athena" / "state.json")
    assert persisted.task_research_ref == supervisor.state.task_research_ref
    assert persisted.task_research_task == "inspect competition"


@pytest.mark.asyncio
async def test_dispatch_general_heals_broken_cached_artifact(tmp_path: Path) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)
    broken_ref = "sha256:" + "e" * 64
    supervisor.state.task_research_task = "inspect competition"
    supervisor.state.task_research_ref = broken_ref
    calls: list[tuple[str, str | None]] = []

    async def fake_general(task: str, prior_agent_id: str | None):
        calls.append((task, prior_agent_id))
        return GeneralTurnOutcome(
            agent_id="general-fresh", result={"result": "fresh", "files": []}
        )

    supervisor._run_general_turn = fake_general  # type: ignore[method-assign]

    result = await supervisor.dispatch_general("inspect competition")

    assert result == {"result": "fresh", "files": []}
    assert calls == [("inspect competition", None)]
    assert supervisor.state.task_research_ref != broken_ref
    persisted = ResearchState.load(tmp_path / ".athena" / "state.json")
    assert persisted.task_research_ref == supervisor.state.task_research_ref


@pytest.mark.asyncio
async def test_set_kaggle_enabled_persists_and_restores_decision(
    tmp_path: Path,
) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)

    result = await supervisor.set_kaggle_enabled(True, download=False)

    assert result == {"kaggle_enabled": True, "download": False}
    persisted = ResearchState.load(tmp_path / ".athena" / "state.json")
    assert persisted.kaggle_download is False
    restored = _checkpoint_supervisor(tmp_path)
    assert restored.kaggle_enabled is True
    assert restored.kaggle_download is False


@pytest.mark.asyncio
async def test_checkpoint_evaluator_persists_frozen_bundle(tmp_path: Path) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)
    ref = await supervisor._store.put_text('{"frozen": true}')

    result = await supervisor.checkpoint_evaluator(ref)

    assert result == {"evaluator_ref": ref}
    assert supervisor.evaluator_ref == ref
    restored = _checkpoint_supervisor(tmp_path)
    assert restored.evaluator_ref == ref


@pytest.mark.asyncio
async def test_run_prepare_skips_when_trusted_baseline_exists(
    tmp_path: Path, monkeypatch
) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)
    monkeypatch.setattr(supervisor.tree, "best_experiment_id", lambda: "exp_baseline")
    calls: list[str] = []

    async def raise_prepare():
        calls.append("prepare")
        raise AssertionError("PREPARE must be skipped")

    supervisor._run_prepare_phase = raise_prepare  # type: ignore[method-assign]
    captured: list[tuple[str, dict[str, object]]] = []
    original_publish = supervisor._publish

    async def publish(kind, payload):
        captured.append((kind, payload))
        return await original_publish(kind, payload)

    supervisor._publish = publish  # type: ignore[method-assign]

    await supervisor._run_prepare()

    assert calls == []
    assert supervisor.state.phase == "SEARCH"
    assert supervisor.state.status == "RUNNING"
    assert any(
        kind == "output" and "跳过 PREPARE" in str(payload.get("text"))
        for kind, payload in captured
    )

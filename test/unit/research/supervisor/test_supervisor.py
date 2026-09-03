"""Single-writer Supervisor Plan creation contracts."""

import asyncio
import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents.task_agents import register_plan_agent
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
from athena.research.prepare.authority import BaselineAuthorityError
from athena.research.supervisor.deps import (
    PhaseActions,
    ResearchActions,
    SearchServices,
    SupervisorDeps,
    SupervisorPaths,
    SupervisorRuntime,
)
from athena.research.supervisor.plans import PlanInput, PlanState
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduling import Scheduler
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
        task_text="Improve the held-out score.",
        task_understanding={"goal": "Improve the held-out score."},
    )
    task_handoff = "# Confirmed task\n\nImprove the held-out score.\n"
    task_handoff_ref = await store.put_text(task_handoff)
    state.handoff_refs["task_clarification"] = task_handoff_ref
    handoff_dir = tmp_path / ".athena" / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "TASK_CLARIFICATION.md").write_text(task_handoff, encoding="utf-8")
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
        state=state,
        tree=tree,
        deps=SupervisorDeps(
            paths=SupervisorPaths(
                project_root=tmp_path,
                state_path=tmp_path / ".athena" / "state.json",
                tree_path=tmp_path / ".athena" / "research_tree.json",
            ),
            runtime=SupervisorRuntime(
                store=store, agents=agents, workspaces=workspaces
            ),
            research=ResearchActions(
                plan=unused_plan_turn, supervisor=unused_supervisor_turn
            ),
            phases=PhaseActions(publish=publish),
            search=SearchServices(
                scheduler=Scheduler(),
                recovery=Recovery(),
            ),
        ),
    )
    supervisor_holder["supervisor"] = supervisor
    await supervisor.record_guidance("use ViT next", scope="next")
    await supervisor.start_plan("hyp_vit")

    frozen = await supervisor.plan_input("hyp_vit")
    assert frozen.hypothesis is not None
    assert frozen.hypothesis.id == "hyp_vit"
    assert frozen.evaluator_ref == evaluator_ref
    assert frozen.human_context == "use ViT next"
    assert frozen.task_context == (
        "--- Confirmed task contract (authoritative) ---\n"
        "# Confirmed task\n\nImprove the held-out score.\n"
        "--- end of confirmed task contract ---"
    )
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
        persisted_tree.get_experiment("exp_hyp_vit").status is ExperimentStatus.RUNNING
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


def _checkpoint_supervisor(
    tmp_path: Path,
    run_general_turn=None,
    run_prepare_phase=None,
    prepare_resume_is_attested=None,
    publish_callback=None,
    runtime_agents=None,
) -> Supervisor:
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

    publish_callback = publish_callback or publish

    return Supervisor(
        state=state,
        tree=ResearchTree(),
        deps=SupervisorDeps(
            paths=SupervisorPaths(
                project_root=tmp_path,
                state_path=state_path,
                tree_path=tmp_path / ".athena" / "research_tree.json",
            ),
            runtime=SupervisorRuntime(
                store=LocalArtifactStore(tmp_path / "artifacts"),
                agents=runtime_agents or SimpleNamespace(),
                workspaces=SimpleNamespace(),
            ),
            research=ResearchActions(
                plan=no_plan_turn,
                supervisor=no_supervisor_turn,
                general=run_general_turn,
            ),
            phases=PhaseActions(
                publish=publish_callback,
                prepare=run_prepare_phase,
                prepare_resume_is_attested=prepare_resume_is_attested,
            ),
            search=SearchServices(
                scheduler=Scheduler(),
                recovery=Recovery(),
            ),
        ),
    )


async def _supervisor_with_frozen_plan(
    tmp_path: Path,
    agents: object,
    *,
    task_context: str = "confirmed context",
) -> Supervisor:
    supervisor = _checkpoint_supervisor(tmp_path, runtime_agents=agents)
    store = supervisor._deps.runtime.store
    evaluator_ref = await store.put_text("evaluator")
    tree_ref = await store.put_text("tree")
    context_ref = await store.put_text(
        PlanInput(
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task_context=task_context,
        ).model_dump_json()
    )
    supervisor.state.phase = "SEARCH"
    supervisor.state.plans["hyp_failure"] = PlanState(
        kind="SEARCH",
        context_ref=context_ref,
        turns_used=0,
        turn_limit=4,
        patience=2,
    )
    return supervisor


class _InfrastructureFailureAgents:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.task: dict[str, object] | None = None

    async def followup(self, _plan_id: str, task: dict[str, object]) -> str:
        self.task = task
        if self.stage == "followup":
            raise RuntimeError("followup unavailable")
        return "run-1"

    async def wait_run(self, _run_id: str):
        if self.stage == "wait_run":
            raise RuntimeError("wait unavailable")
        return SimpleNamespace(status="completed", response_ref="{not-json")


def test_configure_options_updates_focused_dependencies(tmp_path: Path) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)

    supervisor.configure_options(
        direction="minimize", tolerance=0.05, auto_validate=True
    )

    assert supervisor._deps.search.direction == "minimize"
    assert supervisor._deps.search.tolerance == 0.05
    assert supervisor._deps.phases.auto_validate is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "diagnostic"),
    [
        ("followup", "RuntimeError: followup unavailable"),
        ("wait_run", "RuntimeError: wait unavailable"),
        ("parse", "JSONDecodeError:"),
    ],
)
async def test_plan_turn_infrastructure_failure_is_durable(
    tmp_path: Path,
    stage: str,
    diagnostic: str,
) -> None:
    agents = _InfrastructureFailureAgents(stage)
    supervisor = await _supervisor_with_frozen_plan(tmp_path, agents)

    completed = await supervisor._plans.run_turn("hyp_failure")

    assert completed.decision is None
    assert completed.result is None
    assert completed.failure is not None
    assert completed.failure.kind == "turn_infrastructure_failed"
    assert diagnostic in completed.failure.detail
    persisted = ResearchState.load(tmp_path / ".athena" / "state.json")
    failure = persisted.plans["hyp_failure"].last_failure
    assert failure is not None
    assert failure.startswith("turn_infrastructure_failed: ")
    assert diagnostic in failure


@pytest.mark.asyncio
async def test_plan_turn_separates_confirmed_context_from_turn_header(
    tmp_path: Path,
) -> None:
    agents = _InfrastructureFailureAgents("followup")
    supervisor = await _supervisor_with_frozen_plan(
        tmp_path,
        agents,
        task_context="--- Confirmed task contract (authoritative) ---\nbody",
    )

    await supervisor._plans.run_turn("hyp_failure")

    assert agents.task is not None
    assert "stale rounds: 0.\n\n--- Confirmed task contract (authoritative) ---" in str(
        agents.task["content"]
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
    broken_ref = "sha256:" + "e" * 64
    calls: list[tuple[str, str | None]] = []

    async def fake_general(task: str, prior_agent_id: str | None):
        calls.append((task, prior_agent_id))
        return GeneralTurnOutcome(
            agent_id="general-fresh", result={"result": "fresh", "files": []}
        )

    supervisor = _checkpoint_supervisor(tmp_path, fake_general)
    supervisor.state.task_research_task = "inspect competition"
    supervisor.state.task_research_ref = broken_ref

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
    ref = await supervisor._deps.runtime.store.put_text('{"frozen": true}')

    result = await supervisor.checkpoint_evaluator(ref)

    assert result == {"evaluator_ref": ref}
    assert supervisor.evaluator_ref == ref
    restored = _checkpoint_supervisor(tmp_path)
    assert restored.evaluator_ref == ref


def test_evaluator_properties_share_research_state_as_canonical_owner(
    tmp_path: Path,
) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)
    search_ref = "sha256:" + "a" * 64
    final_ref = "sha256:" + "b" * 64

    supervisor.evaluator_ref = search_ref
    supervisor.final_evaluator_ref = final_ref

    assert supervisor.state.evaluator_ref == search_ref
    assert supervisor.state.final_evaluator_ref == final_ref
    supervisor.state.evaluator_ref = None
    supervisor.state.final_evaluator_ref = None
    assert supervisor.evaluator_ref is None
    assert supervisor.final_evaluator_ref is None


@pytest.mark.asyncio
async def test_recover_rebinds_transient_reads_to_distinct_state(
    tmp_path: Path,
) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)
    recovered_state = ResearchState(
        status="STOPPED",
        phase="SEARCH",
        search_limit=3,
        concurrency=1,
        kaggle_download=False,
    )

    recovered = await supervisor.recover(recovered_state)

    assert recovered is supervisor.state
    assert recovered is not recovered_state
    assert supervisor.is_stopped() is True
    assert supervisor.kaggle_enabled is True
    assert supervisor.kaggle_download is False


@pytest.mark.asyncio
async def test_run_prepare_skips_when_trusted_baseline_exists(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    async def raise_prepare():
        calls.append("prepare")
        raise AssertionError("PREPARE must be skipped")

    captured: list[tuple[str, dict[str, object]]] = []

    async def publish(kind, payload):
        captured.append((kind, payload))

    async def attested() -> bool:
        calls.append("attested")
        return True

    supervisor = _checkpoint_supervisor(
        tmp_path,
        run_prepare_phase=raise_prepare,
        prepare_resume_is_attested=attested,
        publish_callback=publish,
    )
    monkeypatch.setattr(supervisor.tree, "best_experiment_id", lambda: "exp_baseline")

    await supervisor._phases._run_prepare()

    assert calls == ["attested"]
    assert supervisor.state.phase == "SEARCH"
    assert supervisor.state.status == "RUNNING"
    assert any(
        kind == "output" and "跳过 PREPARE" in str(payload.get("text"))
        for kind, payload in captured
    )


@pytest.mark.asyncio
async def test_run_prepare_rejects_unattested_local_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_calls: list[str] = []
    published: list[tuple[str, dict[str, object]]] = []

    async def forbidden_prepare():
        prepare_calls.append("prepare")
        raise AssertionError("unattested local baseline must not rerun or skip PREPARE")

    async def unattested() -> bool:
        return False

    async def publish(kind, payload):
        published.append((kind, payload))

    supervisor = _checkpoint_supervisor(
        tmp_path,
        run_prepare_phase=forbidden_prepare,
        prepare_resume_is_attested=unattested,
        publish_callback=publish,
    )
    monkeypatch.setattr(supervisor.tree, "best_experiment_id", lambda: "exp_baseline")

    with pytest.raises(BaselineAuthorityError, match="not attested"):
        await supervisor._phases._run_prepare()

    assert supervisor.state.phase == "PREPARE"
    assert prepare_calls == []
    assert published == []


@pytest.mark.asyncio
async def test_prepare_failure_is_observable_and_retry_enters_running(
    tmp_path: Path,
) -> None:
    statuses: list[str] = []
    supervisor: Supervisor

    async def fail_prepare():
        statuses.append(supervisor.state.status)
        raise RuntimeError(f"prepare attempt {len(statuses)} failed")

    supervisor = _checkpoint_supervisor(tmp_path, run_prepare_phase=fail_prepare)

    with pytest.raises(RuntimeError, match="prepare attempt 1 failed"):
        await supervisor.start()
    assert supervisor.state.phase == "PREPARE"
    assert supervisor.state.status == "FAILED"

    with pytest.raises(RuntimeError, match="prepare attempt 2 failed"):
        await supervisor.start()
    assert statuses == ["RUNNING", "RUNNING"]
    assert supervisor.state.status == "FAILED"


@pytest.mark.asyncio
async def test_later_phase_failure_keeps_recovery_boundary(
    tmp_path: Path,
) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)
    supervisor.state.phase = "VALIDATE"

    async def recovered() -> ResearchState:
        return supervisor.state

    supervisor.recover = recovered  # type: ignore[method-assign]

    await supervisor.start()

    assert supervisor.state.phase == "VALIDATE"
    assert supervisor.state.status == "FAILED"


@pytest.mark.asyncio
async def test_search_turn_crash_is_durable_and_observable(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    async def publish(kind, payload):
        events.append((kind, payload))

    supervisor = _checkpoint_supervisor(tmp_path, publish_callback=publish)
    supervisor.state.phase = "SEARCH"
    supervisor.state.status = "RUNNING"
    supervisor.state.plans["hyp_crash"] = PlanState(
        kind="SEARCH",
        context_ref="sha256:" + "a" * 64,
        turns_used=1,
        turn_limit=4,
        patience=2,
    )

    async def fail_turn():
        raise RuntimeError("worker disconnected")

    async def no_fill() -> bool:
        return False

    task = asyncio.create_task(fail_turn())
    supervisor._run.add_running("hyp_crash", task)
    supervisor._search._fill_slots = no_fill

    await supervisor.run_search()

    restored = ResearchState.load(tmp_path / ".athena" / "state.json")
    assert restored.status == "WAITING"
    assert restored.plans["hyp_crash"].last_failure == (
        "turn_crashed: RuntimeError: worker disconnected"
    )
    assert any(
        kind == "output"
        and payload.get("channel") == "error"
        and "worker disconnected" in str(payload.get("text"))
        for kind, payload in events
    )


@pytest.mark.asyncio
async def test_stop_parks_an_active_run_without_a_second_stop_flag(
    tmp_path: Path,
) -> None:
    supervisor = _checkpoint_supervisor(tmp_path)

    await supervisor.stop()

    assert supervisor.state.status == "WAITING"
    assert ResearchState.load(tmp_path / ".athena" / "state.json").status == "WAITING"

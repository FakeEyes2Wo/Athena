"""Single-writer Supervisor Plan creation contracts."""

import json
from importlib import import_module
from pathlib import Path

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
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState


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
    await supervisor.stop()
    await agents.aclose()

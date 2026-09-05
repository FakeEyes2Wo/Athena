"""SEARCH Plan turn 事件转发：text_delta 在 React 循环中实时露出。

回归：`PlanLifecycle.run_turn` 必须把 plan agent 的 journal 事件（至少
``agent/text_delta``）在 turn 进行中转发给 runtime 发布者，而不是等整段
turn 结束后一次性输出。
"""

import json
from pathlib import Path

import pytest

from test.support import RecordingDocumentProjector

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
from athena.research.supervisor.deps import (
    PhaseActions,
    ResearchActions,
    SearchServices,
    SupervisorDeps,
    SupervisorPaths,
    SupervisorRuntime,
)
from athena.research.supervisor.experiment import PlanTurnResult
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduling import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor


class _StreamingPlanProvider:
    """Plan agent provider：推理文本 + 结构化决策，全部作为流式 text_delta 露出。"""

    model_name = "plan-stream-test"

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        payload = json.dumps(
            {"decision": "submit", "reason": "ready"}, ensure_ascii=False
        )
        yield StreamEvent(
            kind="text_delta", data={"delta": "先检查特征", "accumulated": "先检查特征"}
        )
        yield StreamEvent(
            kind="text_delta", data={"delta": payload, "accumulated": payload}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


@pytest.mark.asyncio
async def test_plan_turn_forwards_text_delta_to_runtime_publisher(
    tmp_path: Path, monkeypatch
) -> None:
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
    state.handoff_refs["task_clarification"] = await store.put_text(task_handoff)
    handoff_dir = tmp_path / ".athena" / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "TASK_CLARIFICATION.md").write_text(task_handoff, encoding="utf-8")

    registry = AgentTypeRegistry()
    supervisor_holder: dict[str, object] = {}
    execution = ExecutionRuntime(project_root=tmp_path, store=store)
    register_plan_agent(
        registry,
        provider=_StreamingPlanProvider(),
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

    async def stub_plan_turn(_plan_id, _state):
        return PlanTurnResult(kind="execution_failed")

    async def unused_supervisor_turn(_text):
        raise AssertionError("SupervisorAgent turn must not run")

    async def publish(_kind, _payload):
        return None

    forwarded: list[tuple[str, str, dict | None]] = []

    async def publish_agent_event(plan_id, kind, event_ref, data):
        forwarded.append((plan_id, kind, data))

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
                store=store,
                agents=agents,
                workspaces=workspaces,
                documents=RecordingDocumentProjector(),
            ),
            research=ResearchActions(
                plan=stub_plan_turn, supervisor=unused_supervisor_turn
            ),
            phases=PhaseActions(
                publish=publish, publish_agent_event=publish_agent_event
            ),
            search=SearchServices(
                scheduler=Scheduler(),
                recovery=Recovery(),
            ),
        ),
    )
    supervisor_holder["supervisor"] = supervisor
    await supervisor.start_plan("hyp_vit")

    completed = await supervisor._plans.run_turn("hyp_vit")

    text_events = [event for event in forwarded if event[1] == "agent/text_delta"]
    assert text_events, "plan turn 必须把 agent text_delta 转发给 runtime 发布者"
    deltas = [event[2].get("delta") for event in text_events]
    assert "先检查特征" in deltas
    assert completed.decision is not None
    assert completed.decision.decision == "submit"
    await supervisor.stop()
    await agents.aclose()

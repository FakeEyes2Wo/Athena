"""SEARCH settlement and crash-recovery integration contracts."""

import asyncio
import json
import shutil

import pytest

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.git_workspace import LocalGitWorkspace
from athena.research.supervisor.deps import (
    PhaseActions,
    ResearchActions,
    SearchServices,
    SupervisorDeps,
    SupervisorPaths,
    SupervisorRuntime,
)
from athena.research.supervisor.experiment import load_best
from athena.research.supervisor.policy import EloPolicy
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from test.integration.research.test_rolling_search import (
    _ControlledRunner,
    _eventually,
    _Harness,
    harness,
)

assert harness


async def _start_one(harness: _Harness, plan_id: str = "h1") -> None:
    harness.state.search_limit = 1
    harness.state.concurrency = 1
    harness.task = asyncio.create_task(harness.supervisor.run_search())
    await _eventually(lambda: plan_id in harness.gates)


async def _wait_next_turn(harness: _Harness, plan_id: str, turn: int) -> None:
    await _eventually(
        lambda: (
            harness.started_turns[plan_id] >= turn
            and not harness.gates[plan_id].is_set()
        )
    )


@pytest.mark.asyncio
async def test_submit_settles_historical_best_not_branch_tip(harness: _Harness):
    await _start_one(harness)
    await harness.finish("h1", 0.85, decision="continue")
    await _wait_next_turn(harness, "h1", 2)
    await harness.finish("h1", 0.82, decision="submit")
    await _eventually(lambda: "h1" not in harness.state.plans)

    experiment_id = harness.tree.experiment_for_hypothesis("h1")
    experiment = harness.tree.get_experiment(experiment_id)
    assert experiment.commit == "commit-0.85"
    assert experiment.eval.primary == 0.85
    best_evidence = await harness.store.get_text(experiment.artifacts["evidence"])
    assert '"metric": 0.85' in best_evidence
    best_report_ref = __import__("json").loads(best_evidence)["report_ref"]
    assert experiment.artifacts["report"] == best_report_ref


@pytest.mark.asyncio
async def test_patience_exhaustion_settles_historical_best(harness: _Harness):
    await _start_one(harness)
    for turn, metric in enumerate((0.85, 0.84, 0.83), start=1):
        await harness.finish("h1", metric, decision="continue")
        if turn < 3:
            await _wait_next_turn(harness, "h1", turn + 1)
    await _eventually(lambda: "h1" not in harness.state.plans)

    experiment = harness.tree.get_experiment(
        harness.tree.experiment_for_hypothesis("h1")
    )
    assert experiment.commit == "commit-0.85"


@pytest.mark.asyncio
async def test_turn_exhaustion_with_best_settles(harness: _Harness):
    harness.tree.get_hypothesis("h1").turn_limit = 2
    await _start_one(harness)
    await harness.finish("h1", 0.81, decision="continue")
    await _wait_next_turn(harness, "h1", 2)
    await harness.finish("h1", 0.82, decision="continue")
    await _eventually(lambda: "h1" not in harness.state.plans)

    experiment = harness.tree.get_experiment(
        harness.tree.experiment_for_hypothesis("h1")
    )
    assert experiment.eval.primary == 0.82


@pytest.mark.asyncio
async def test_turn_exhaustion_without_best_waits_and_releases_slot(
    harness: _Harness,
):
    harness.tree.get_hypothesis("h1").turn_limit = 1
    await _start_one(harness)
    await harness.finish("h1", None, decision="continue", kind="execution_failed")
    await _eventually(lambda: harness.state.status == "WAITING")

    assert "h1" in harness.state.plans
    assert harness.supervisor.running_plan_ids == ()


@pytest.mark.asyncio
async def test_abandon_without_best_marks_inconclusive(harness: _Harness):
    await _start_one(harness)
    await harness.finish("h1", None, decision="abandon", kind="execution_failed")
    await _eventually(lambda: "h1" not in harness.state.plans)

    experiment = harness.tree.get_experiment(
        harness.tree.experiment_for_hypothesis("h1")
    )
    assert experiment.status.value == "FAILED"
    hypothesis = harness.tree.get_hypothesis("h1")
    # 无有效 test 证据不按胜负更新评级：状态为 INCONCLUSIVE，优先级保持不变。
    assert hypothesis.status == "INCONCLUSIVE"
    assert hypothesis.priority == 1000.0
    assert "h1" not in harness.state.plans


@pytest.mark.asyncio
async def test_execution_provider_and_infrastructure_failures_do_not_consume_patience(
    harness: _Harness,
):
    await _start_one(harness)
    await harness.finish("h1", 0.81, decision="continue")
    await _wait_next_turn(harness, "h1", 2)
    for turn, kind in enumerate(
        ("execution_failed", "evaluator_infrastructure_failed"), start=2
    ):
        await harness.finish("h1", None, decision="continue", kind=kind)
        await _wait_next_turn(harness, "h1", turn + 1)

    assert harness.state.plans["h1"].stale_rounds == 0
    best = await load_best(harness.state.plans["h1"].best_ref, harness.store)
    assert best.metric == 0.81


@pytest.mark.asyncio
async def test_provider_failure_at_turn_limit_waits_without_consuming_patience(
    harness: _Harness,
):
    harness.tree.get_hypothesis("h1").turn_limit = 1
    harness.crash_ids.add("h1")
    harness.state.search_limit = 1
    harness.state.concurrency = 1
    harness.task = asyncio.create_task(harness.supervisor.run_search())
    await _eventually(lambda: harness.state.status == "WAITING")

    assert harness.state.status == "WAITING"
    assert harness.state.plans["h1"].stale_rounds == 0
    assert harness.supervisor.running_plan_ids == ()


class InjectedCrash(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_tree_first_crash_settles_once(harness: _Harness, monkeypatch):
    await _start_one(harness)
    original_save = ResearchState.save

    def crash_after_tree(state, path):
        if (
            "h1" not in state.plans
            and (harness.root / ".athena" / "research_tree.json").is_file()
        ):
            raise InjectedCrash("after tree settlement")
        return original_save(state, path)

    monkeypatch.setattr(ResearchState, "save", crash_after_tree)
    await harness.finish("h1", 0.81)
    await _eventually(lambda: harness.task.done())
    monkeypatch.setattr(ResearchState, "save", original_save)

    persisted_state = ResearchState.load(harness.root / ".athena" / "state.json")
    assert "h1" in persisted_state.plans
    recovered = await harness.supervisor.recover(persisted_state)

    assert "h1" not in recovered.plans
    assert (
        len(
            [
                exp
                for exp in harness.supervisor.tree.experiments()
                if exp.hypothesis_id == "h1"
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_restart_resumes_stable_agent_context_and_workspace(harness: _Harness):
    await harness.supervisor.start_plan("h1")
    frozen_context = harness.state.plans["h1"].context_ref
    original_workspace = harness.supervisor.workspace_path("h1")

    registry = AgentTypeRegistry()
    registry.register(
        "plan",
        lambda agent_id, _config=None: AgentSpec(
            runner=_ControlledRunner(agent_id, harness), codec=JsonCodec()
        ),
    )
    agents = AgentRuntime(
        type_registry=registry,
        project_root=harness.root,
        rollout_dir=harness.root / ".athena" / "logs" / "agents",
    )
    agents.start()
    workspaces = LocalGitWorkspace(
        harness.root / "repo", harness.root / "worktrees", harness.store.put_bytes
    )
    policy = EloPolicy()
    state = ResearchState.load(harness.root / ".athena" / "state.json")
    restarted = Supervisor(
        state=state,
        tree=harness.tree,
        deps=SupervisorDeps(
            paths=SupervisorPaths(
                project_root=harness.root,
                state_path=harness.root / ".athena" / "state.json",
                tree_path=harness.root / ".athena" / "research_tree.json",
            ),
            runtime=SupervisorRuntime(
                store=harness.store,
                agents=agents,
                workspaces=workspaces,
            ),
            research=ResearchActions(
                plan=lambda _plan_id, _state: asyncio.sleep(0),
                supervisor=lambda _text: asyncio.sleep(0, result="ok"),
            ),
            phases=PhaseActions(publish=lambda _kind, _payload: asyncio.sleep(0)),
            search=SearchServices(
                scheduler=Scheduler(policy),
                recovery=Recovery(),
            ),
        ),
    )
    restart_task = asyncio.create_task(restarted.start())
    try:
        await _eventually(lambda: "h1" in restarted.running_plan_ids)

        assert restarted.state.status == "RUNNING"
        assert restarted.state.plans["h1"].context_ref == frozen_context
        assert restarted.workspace_path("h1") == original_workspace
        assert agents.has_agent("h1")
    finally:
        await asyncio.wait_for(restarted.stop(), timeout=2)
        await asyncio.wait_for(
            asyncio.gather(restart_task, return_exceptions=True), timeout=2
        )
        await asyncio.wait_for(agents.aclose(), timeout=2)


@pytest.mark.asyncio
async def test_recover_rebuilds_experiment_lost_before_tree_save(harness: _Harness):
    await harness.supervisor.start_plan("h1")
    tree_path = harness.root / ".athena" / "research_tree.json"
    payload = json.loads(tree_path.read_text(encoding="utf-8"))
    del payload["experiments"]["exp_h1"]
    tree_path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = await harness.supervisor.recover(
        ResearchState.load(harness.root / ".athena" / "state.json")
    )

    assert "h1" in recovered.plans
    assert harness.supervisor.tree.experiment_for_hypothesis("h1") == "exp_h1"
    assert harness.supervisor.tree.get_experiment("exp_h1").status.value == "RUNNING"
    persisted = json.loads(tree_path.read_text(encoding="utf-8"))
    assert "exp_h1" in persisted["experiments"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["context", "workspace"])
async def test_missing_context_or_workspace_waits_without_recreation(
    harness: _Harness, missing: str
):
    await harness.supervisor.start_plan("h1")
    context_ref = harness.state.plans["h1"].context_ref
    workspace = harness.supervisor.workspace_path("h1")
    if missing == "context":
        harness.store.path_for(context_ref).unlink()
    else:
        shutil.rmtree(workspace)

    recovered = await harness.supervisor.recover(
        ResearchState.load(harness.root / ".athena" / "state.json")
    )

    assert recovered.status == "WAITING"
    assert "h1" in recovered.plans
    assert harness.supervisor.workspace_path("h1") == workspace

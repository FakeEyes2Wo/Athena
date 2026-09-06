"""Rolling SEARCH integration over real durable infrastructure."""

import asyncio
import json
from collections import defaultdict, deque
from pathlib import Path

import pytest

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.supervisor.deps import (
    PhaseActions,
    ResearchActions,
    SearchServices,
    SupervisorDeps,
    SupervisorPaths,
    SupervisorRuntime,
)
from athena.research.supervisor.experiment import PlanTurnResult, apply_trusted_score
from athena.research.supervisor.plans import PlanBest
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduling import (
    EloPolicy,
    Scheduler,
    count_search_attempts,
)
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from tests.support import RecordingDocumentProjector


async def _eventually(predicate, timeout: float = 20) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class _ControlledRunner:
    def __init__(self, plan_id: str, harness: "_Harness") -> None:
        self._plan_id = plan_id
        self._harness = harness

    async def run(self, _request, *, session, emit):
        del session, emit
        self._harness.started.add(self._plan_id)
        self._harness.started_turns[self._plan_id] += 1
        if self._plan_id in self._harness.crash_ids:
            raise RuntimeError("injected dispatch crash")
        gate = asyncio.Event()
        self._harness.gates[self._plan_id] = gate
        await gate.wait()
        decision = self._harness.decisions[self._plan_id].popleft()
        ref = await self._harness.store.put_text(
            json.dumps({"decision": decision, "reason": "controlled completion"})
        )
        return {"result_ref": ref}


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        direction: str = "maximize",
        tolerance: float = 0.0,
        policy: EloPolicy | None = None,
    ) -> None:
        self.root = tmp_path
        self.direction = direction
        self.tolerance = tolerance
        self.policy = policy or EloPolicy()
        self.store = LocalArtifactStore(tmp_path / "artifacts")
        self.tree = ResearchTree()
        self.state = ResearchState(
            status="RUNNING",
            phase="SEARCH",
            search_limit=6,
            concurrency=4,
        )
        self.started: set[str] = set()
        self.gates: dict[str, asyncio.Event] = {}
        self.started_turns: dict[str, int] = defaultdict(int)
        self.decisions: dict[str, deque[str]] = defaultdict(deque)
        self.outcomes: dict[str, deque[tuple[str, float | None]]] = defaultdict(deque)
        self.crash_ids: set[str] = set()
        self.events: list[tuple[str, dict[str, object]]] = []
        self.task: asyncio.Task[None] | None = None
        self.supervisor_turn = lambda _text: asyncio.sleep(0, result="ok")

    async def _persist_confirmed_task_context(self) -> None:
        """Create the real durable task context required by every SEARCH Plan."""
        handoff = "# Confirmed task\n\nImprove the controlled held-out score.\n"
        self.state.task_text = "Improve the controlled held-out score."
        self.state.task_understanding = {
            "goal": "Improve the controlled held-out score."
        }
        self.state.handoff_refs["task_clarification"] = await self.store.put_text(
            handoff
        )
        handoff_dir = self.root / ".athena" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        (handoff_dir / "TASK_CLARIFICATION.md").write_text(handoff, encoding="utf-8")

    async def initialize(self) -> None:
        await self._persist_confirmed_task_context()
        self.git = LocalGitWorkspace(
            self.root / "repo", self.root / "worktrees", self.store.put_bytes
        )
        self.base_commit = await self.git.init()
        self.evaluator_ref = await self.store.put_text('{"frozen":true}')
        self.evidence_ref = await self.store.put_text("baseline evidence")
        self.tree.add_hypothesis(
            Hypothesis(
                id="baseline",
                statement="baseline",
                intervention="fit baseline",
                expected_effect="establish reference",
            )
        )
        self.tree.add_experiment(
            "exp_baseline",
            Experiment(
                hypothesis_id="baseline",
                commit=self.base_commit,
                plan=ExperimentPlan(
                    kind="baseline",
                    change="baseline",
                    run_config_ref=self.evaluator_ref,
                    budget={},
                    acceptance_rule="trusted score",
                ),
                gitwork=GitWorkBranch(
                    path=str(self.root / "repo"),
                    branch="main",
                    base_commit=self.base_commit,
                ),
                status=ExperimentStatus.SUCCEEDED,
                eval=EvalResult(
                    experiment_id="exp_baseline",
                    primary=0.8,
                    per_sample=self.evidence_ref,
                ),
            ),
        )
        self.tree.set_sota("exp_baseline")
        for index in range(1, 7):
            self.add_hypothesis(f"h{index}")

        registry = AgentTypeRegistry()
        registry.register(
            "plan",
            lambda agent_id: AgentSpec(
                runner=_ControlledRunner(agent_id, self), codec=JsonCodec()
            ),
        )
        self.agents = AgentRuntime(
            type_registry=registry,
            project_root=self.root,
            rollout_dir=self.root / ".athena" / "logs" / "agents",
        )
        self.agents.start()

        async def run_plan_turn(plan_id, state):
            kind, metric = self.outcomes[plan_id].popleft()
            if kind != "scored":
                return PlanTurnResult(kind=kind, error=f"controlled {kind}")
            assert metric is not None
            report_ref = await self.store.put_text(f"report {plan_id} {metric}")
            evidence_ref = await self.store.put_text(
                json.dumps(
                    {
                        "plan": plan_id,
                        "metric": metric,
                        "commit": f"commit-{metric}",
                        "report_ref": report_ref,
                    }
                )
            )
            next_state = await apply_trusted_score(
                state,
                PlanBest(
                    metric=metric,
                    commit=f"commit-{metric}",
                    evidence_ref=evidence_ref,
                ),
                store=self.store,
            )
            return PlanTurnResult(
                kind="scored",
                metric=metric,
                commit=f"commit-{metric}",
                next_state=next_state,
                predictions_ref=evidence_ref,
                evidence_ref=evidence_ref,
                report_ref=report_ref,
            )

        async def publish(kind, payload):
            self.events.append((kind, payload))

        self.supervisor = Supervisor(
            state=self.state,
            tree=self.tree,
            deps=SupervisorDeps(
                paths=SupervisorPaths(
                    project_root=self.root,
                    state_path=self.root / ".athena" / "state.json",
                    tree_path=self.root / ".athena" / "research_tree.json",
                ),
                runtime=SupervisorRuntime(
                    store=self.store,
                    agents=self.agents,
                    workspaces=self.git,
                    documents=RecordingDocumentProjector(),
                ),
                research=ResearchActions(
                    plan=run_plan_turn,
                    supervisor=lambda text: self.supervisor_turn(text),
                ),
                phases=PhaseActions(publish=publish),
                search=SearchServices(
                    scheduler=Scheduler(self.policy),
                    recovery=Recovery(),
                    direction=self.direction,
                    tolerance=self.tolerance,
                ),
            ),
        )

    def add_hypothesis(self, plan_id: str, *, priority: float = 1000.0) -> None:
        self.tree.add_hypothesis(
            Hypothesis(
                id=plan_id,
                parent_id="exp_baseline",
                statement=f"claim {plan_id}",
                intervention=f"change {plan_id}",
                expected_effect="improve score",
                priority=priority,
                turn_limit=4,
                patience=2,
            )
        )

    async def start_search(self) -> None:
        self.task = asyncio.create_task(self.supervisor.run_search())
        await _eventually(lambda: len(self.supervisor.running_plan_ids) == 4)

    async def finish(
        self,
        plan_id: str,
        metric: float | None,
        *,
        decision: str = "submit",
        kind: str = "scored",
    ) -> None:
        self.decisions[plan_id].append(decision)
        self.outcomes[plan_id].append((kind, metric))
        self.gates[plan_id].set()

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        await self.supervisor.stop()
        await self.agents.aclose()


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch) -> _Harness:
    for name, value in {
        "GIT_AUTHOR_NAME": "Athena Test",
        "GIT_AUTHOR_EMAIL": "athena@example.invalid",
        "GIT_COMMITTER_NAME": "Athena Test",
        "GIT_COMMITTER_EMAIL": "athena@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    value = _Harness(tmp_path)
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_first_completion_refills_without_batch_barrier(harness: _Harness):
    await harness.start_search()
    assert set(harness.supervisor.running_plan_ids) == {"h1", "h2", "h3", "h4"}

    await harness.finish("h2", 0.82)
    await _eventually(lambda: "h5" in harness.supervisor.running_plan_ids)

    assert set(harness.supervisor.running_plan_ids) == {"h1", "h3", "h4", "h5"}


@pytest.mark.asyncio
async def test_four_running_plans_have_distinct_agent_workspace_and_log_ids(
    harness: _Harness,
):
    await harness.start_search()
    await _eventually(lambda: len(harness.started) == 4)

    identities = [
        harness.supervisor.plan_identity(plan_id) for plan_id in sorted(harness.started)
    ]
    paths = [
        harness.supervisor.workspace_path(plan_id)
        for plan_id in sorted(harness.started)
    ]
    assert [identity["agent"] for identity in identities] == [
        "h1",
        "h2",
        "h3",
        "h4",
    ]
    assert len(set(paths)) == 4
    assert all(harness.agents.has_agent(plan_id) for plan_id in harness.started)
    assert all(
        (harness.root / ".athena" / "logs" / "agents" / f"{plan_id}.jsonl").is_file()
        for plan_id in harness.started
    )


@pytest.mark.asyncio
async def test_stop_interrupts_active_plan_agent_runs(harness: _Harness):
    await harness.start_search()
    await _eventually(lambda: len(harness.started) == 4)
    run_ids = {
        snapshot.agent_id: snapshot.pending_run_id
        for snapshot in harness.agents.list_agents()
        if snapshot.agent_id in harness.started
    }
    assert all(run_ids.values())

    await asyncio.wait_for(harness.supervisor.stop(), timeout=1)
    assert all(
        harness.agents.run_summary(run_id) is None
        for run_id in run_ids.values()
        if run_id is not None
    )
    assert all(not harness.agents.has_agent(plan_id) for plan_id in run_ids)


@pytest.mark.asyncio
async def test_ready_plan_precedes_new_hypothesis(harness: _Harness):
    harness.state.concurrency = 1
    await harness.supervisor.start_plan("h3")
    harness.task = asyncio.create_task(harness.supervisor.run_search())
    await _eventually(lambda: bool(harness.started))

    assert harness.started == {"h3"}


@pytest.mark.asyncio
async def test_human_next_is_consumed_once_without_priority_mutation(harness: _Harness):
    harness.state.concurrency = 1
    before = harness.tree.get_hypothesis("h4").priority
    await harness.supervisor.select_next_hypothesis("h4")
    harness.task = asyncio.create_task(harness.supervisor.run_search())
    await _eventually(lambda: bool(harness.started))

    assert harness.started == {"h4"}
    assert harness.tree.get_hypothesis("h4").priority == before
    assert harness.supervisor.next_hypothesis_id is None


@pytest.mark.asyncio
async def test_attempts_count_created_plans_not_turns_or_scores(harness: _Harness):
    harness.state.search_limit = 1
    harness.state.concurrency = 1
    harness.task = asyncio.create_task(harness.supervisor.run_search())
    await _eventually(lambda: "h1" in harness.started)
    await harness.finish("h1", 0.81, decision="continue")
    await _eventually(lambda: harness.state.plans["h1"].turns_used >= 1)

    assert count_search_attempts(harness.state, harness.tree) == 1


@pytest.mark.asyncio
async def test_out_of_order_results_use_each_frozen_reference(harness: _Harness):
    harness.state.search_limit = 2
    harness.state.concurrency = 2
    harness.task = asyncio.create_task(harness.supervisor.run_search())
    await _eventually(lambda: set(harness.supervisor.running_plan_ids) == {"h1", "h2"})
    await _eventually(lambda: set(harness.gates) == {"h1", "h2"})

    await harness.finish("h2", 0.79)
    await _eventually(lambda: "h2" not in harness.state.plans)
    await harness.finish("h1", 0.81)
    await _eventually(lambda: "h1" not in harness.state.plans)

    assert harness.tree.get_hypothesis("h2").priority == 984.0
    assert harness.tree.get_hypothesis("h1").priority == 1016.0


@pytest.mark.asyncio
async def test_minimize_settlement_uses_frozen_tolerance_and_updates_sota(
    harness: _Harness,
):
    minimized = _Harness(
        harness.root / "minimized", direction="minimize", tolerance=0.01
    )
    await minimized.initialize()
    minimized.state.search_limit = 3
    minimized.state.concurrency = 3
    try:
        minimized.task = asyncio.create_task(minimized.supervisor.run_search())
        await _eventually(
            lambda: set(minimized.supervisor.running_plan_ids) == {"h1", "h2", "h3"}
        )
        await _eventually(lambda: set(minimized.gates) == {"h1", "h2", "h3"})

        await minimized.finish("h1", 0.78)
        await _eventually(lambda: "h1" not in minimized.state.plans)
        await minimized.finish("h2", 0.795)
        await _eventually(lambda: "h2" not in minimized.state.plans)
        await minimized.finish("h3", 0.82)
        await _eventually(lambda: "h3" not in minimized.state.plans)

        assert minimized.tree.get_hypothesis("h1").priority == 1016.0
        assert minimized.tree.get_hypothesis("h2").priority == 1000.0
        assert minimized.tree.get_hypothesis("h3").priority == 984.0
        assert minimized.tree.best_experiment_id() == "exp_h1"
    finally:
        await minimized.close()


@pytest.mark.asyncio
async def test_supervisor_settlement_uses_the_scheduler_policy(harness: _Harness):
    customized = _Harness(harness.root / "custom-policy", policy=EloPolicy(k=64))
    await customized.initialize()
    customized.state.search_limit = 1
    customized.state.concurrency = 1
    try:
        customized.task = asyncio.create_task(customized.supervisor.run_search())
        await _eventually(lambda: "h1" in customized.gates)
        await customized.finish("h1", 0.81)
        await _eventually(lambda: "h1" not in customized.state.plans)

        assert customized.tree.get_hypothesis("h1").priority == 1032.0
    finally:
        await customized.close()


@pytest.mark.asyncio
async def test_turn_is_persisted_before_dispatch(harness: _Harness):
    harness.state.search_limit = 1
    harness.state.concurrency = 1
    harness.crash_ids.add("h1")
    harness.task = asyncio.create_task(harness.supervisor.run_search())
    await _eventually(lambda: "h1" in harness.started)
    persisted = ResearchState.load(harness.root / ".athena" / "state.json")

    assert persisted.plans["h1"].turns_used == 1


@pytest.mark.asyncio
async def test_supervisor_proposal_enters_the_scheduler(harness: _Harness):
    empty = _Harness(harness.root / "generated")
    await empty.initialize()
    empty.tree = ResearchTree.from_dict(
        {
            **empty.tree.to_dict(),
            "hypotheses": {"baseline": empty.tree.to_dict()["hypotheses"]["baseline"]},
        }
    )
    empty.supervisor.tree = empty.tree
    empty.state.search_limit = 1
    empty.state.concurrency = 1
    try:
        proposal = await empty.supervisor.propose_hypothesis(
            statement="attention helps",
            intervention="fit a compact transformer",
            expected_effect="improve trusted accuracy",
            supersedes=[],
            sources=["paper://attention"],
            turn_limit=3,
            patience=2,
        )
        hypothesis_id = str(proposal["hypothesis_id"])
        hypothesis = empty.tree.get_hypothesis(hypothesis_id)
        empty.task = asyncio.create_task(empty.supervisor.run_search())
        await _eventually(lambda: hypothesis_id in empty.started)

        assert hypothesis.parent_id == "exp_baseline"
        assert hypothesis.priority == 1000.0
    finally:
        await empty.close()


@pytest.mark.asyncio
async def test_generated_hypothesis_fills_the_requested_slot(harness: _Harness):
    empty = _Harness(harness.root / "generated-slot")
    await empty.initialize()
    payload = empty.tree.to_dict()
    payload["hypotheses"] = {"baseline": payload["hypotheses"]["baseline"]}
    empty.tree = ResearchTree.from_dict(payload)
    empty.supervisor.tree = empty.tree
    empty.state.search_limit = 1
    empty.state.concurrency = 1

    async def generate(_text: str) -> str:
        await empty.supervisor.propose_hypothesis(
            statement="generated attention claim",
            intervention="fit generated transformer",
            expected_effect="improve trusted score",
            supersedes=[],
            sources=[],
            turn_limit=3,
            patience=2,
        )
        return "generated"

    empty.supervisor_turn = generate
    try:
        empty.task = asyncio.create_task(empty.supervisor.run_search())
        await _eventually(lambda: bool(empty.started))

        assert len(empty.started) == 1
    finally:
        await empty.close()

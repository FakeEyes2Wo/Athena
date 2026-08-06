"""SEARCH 工作流测试：代码路由、监督者决策。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.evaluation.types import (
    ComparisonVerdict,
    EvalResult,
    EvalSpec,
    MetricDef,
)
from athena.experiment.supervisor import Supervisor
from athena.data.types import DataProfile
from athena.ideator import DebateResult
from athena.research.budget import BudgetSnapshot
from athena.workflows.search import code_agent as code_agent_module
from athena.workflows.search.code_agent import (
    CodeAgent,
    CodeExecutionError,
    CodeRouter,
    ProcessResult,
)
from athena.workflows.search.code_agent import CodegenResult
from athena.workflows.search.idea_generation import generate_hypotheses
from athena.workflows.search.search_loop import SearchLoop

COMMIT = "a" * 40
TEST_PROFILE = DataProfile(
    row_count=100,
    col_count=5,
    task_type_hint="classification",
)


class RecordingIdeator:
    def __init__(self) -> None:
        self.calls: list[tuple[DataProfile, list, list, ResearchTree]] = []
        self.result = DebateResult(
            hypotheses=[
                Hypothesis(
                    id=f"hyp-ideator-{index}",
                    parent_id=f"exp-parent-{index}",
                    statement=f"Ideator hypothesis {index}",
                    intervention=f"Apply isolated change {index}",
                    expected_effect="Improve the primary metric",
                    sources=[f"paper-{index}"],
                    evidence_refs=[f"artifact://debate/evidence-{index}"],
                )
                for index in range(1, 4)
            ],
            transcript=[{"stage": "judge", "agent": "judge-0"}],
            failures=[
                {"stage": "review", "agent": "debater-2", "error": "TimeoutError"}
            ],
            artifact_ref="artifact://debate/result",
        )

    async def generate(self, profile, papers, models, tree) -> DebateResult:
        self.calls.append((profile, papers, models, tree))
        return self.result


def experiment_plan() -> ExperimentPlan:
    return ExperimentPlan(
        kind="search",
        change="Train one deterministic model",
        run_config_ref="artifact://runs/exp-test/config",
        budget={"trials": 1},
        acceptance_rule="The frozen evaluator completes",
    )


def _search_tree() -> tuple[ResearchTree, str, str]:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="hyp-baseline",
            statement="Establish a baseline",
            intervention="Train the default model",
            expected_effect="Provide a reference metric",
            status="SUPPORTED",
        )
    )
    tree.add_hypothesis(
        Hypothesis(
            id="hyp-candidate",
            parent_id="exp-baseline",
            statement="Improve the baseline",
            intervention="Add one deterministic feature",
            expected_effect="Increase the primary metric",
        )
    )
    tree.add_experiment(
        "exp-baseline",
        Experiment(
            hypothesis_id="hyp-baseline",
            commit=COMMIT,
            plan=ExperimentPlan(
                kind="baseline",
                change="Train the default model",
                run_config_ref="artifact://runs/exp-baseline/config",
                budget={"max_trials": 1},
                acceptance_rule="Evaluation completes",
            ),
            gitwork=GitWorkBranch(
                path="C:/worktrees/exp-baseline",
                branch="baseline/exp-baseline",
                base_commit=COMMIT,
            ),
        ),
    )
    tree.transition_experiment("exp-baseline", ExperimentStatus.RUNNING)
    tree.complete_experiment(
        "exp-baseline",
        eval=EvalResult(
            experiment_id="exp-baseline",
            primary=0.5,
            per_sample="artifact://samples/exp-baseline",
        ),
        verdict=None,
        artifacts={"logs": "artifact://logs/exp-baseline"},
    )
    tree.set_sota("exp-baseline")
    return tree, "exp-baseline", "hyp-candidate"


class _Workspace:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    async def create(self, base_commit: str, branch: str) -> GitWorkBranch:
        self.created.append((base_commit, branch))
        return GitWorkBranch(
            path="C:/worktrees/candidate", branch=branch, base_commit=base_commit
        )


class _Ranker:
    def __init__(self) -> None:
        self.updates: list[list[tuple[str, str, bool]]] = []

    def select(self, hypotheses, proximity) -> str:
        return "hyp-candidate"

    def update(self, comparisons) -> None:
        self.updates.append(comparisons)


class _Proximity:
    def __init__(self) -> None:
        self.added: list[str] = []

    def add(self, hypothesis: Hypothesis) -> None:
        self.added.append(hypothesis.id)


class _Comparator:
    def __init__(self, verdict: ComparisonVerdict) -> None:
        self.verdict = verdict
        self.calls = []

    def compare(self, baseline, candidate, *, direction):
        self.calls.append((baseline, candidate, direction))
        return self.verdict


class _Supervisor:
    def __init__(self, action: str) -> None:
        self.action = action
        self.calls = []

    def decide(self, verdict, budget):
        self.calls.append((verdict, budget.remaining))
        return SimpleNamespace(action=self.action, reason="test decision")


class _CodeAgent:
    def __init__(self, tree: ResearchTree, *, error: BaseException | None = None):
        self.tree = tree
        self.error = error
        self.observed_id: str | None = None

    async def execute(
        self, experiment_id, hypothesis, plan, parent_commit, eval_spec, worktree
    ) -> CodegenResult:
        self.observed_id = experiment_id
        assert (
            self.tree.get_experiment(experiment_id).status is ExperimentStatus.RUNNING
        )
        if self.error is not None:
            raise self.error
        return CodegenResult(
            experiment_id=experiment_id,
            commit=parent_commit,
            diff=f"artifact://diffs/{experiment_id}",
            logs=f"artifact://logs/{experiment_id}",
            eval=EvalResult(
                experiment_id=experiment_id,
                primary=0.6,
                per_sample=f"artifact://samples/{experiment_id}",
            ),
        )


def _loop(
    tree: ResearchTree,
    *,
    verdict: ComparisonVerdict | None = None,
    action: str = "ACCEPT",
    error: BaseException | None = None,
):
    workspace = _Workspace()
    ranker = _Ranker()
    proximity = _Proximity()
    comparator = _Comparator(
        verdict or ComparisonVerdict(winner="candidate", p_value=0.01)
    )
    supervisor = _Supervisor(action)
    code_agent = _CodeAgent(tree, error=error)
    budget = BudgetSnapshot(remaining=1, max_no_improve=5)
    loop = SearchLoop(
        tree,
        workspace,
        EvalSpec(
            primary=MetricDef(
                name="f1_macro", direction="maximize", description="Macro F1"
            )
        ),
        budget,
        data_profile=TEST_PROFILE,
        ranker=ranker,
        proximity=proximity,
        comparator=comparator,
        supervisor=supervisor,
        code_agent=code_agent,
    )
    return (
        loop,
        workspace,
        ranker,
        proximity,
        comparator,
        supervisor,
        code_agent,
        budget,
    )


@pytest.mark.asyncio
async def test_generate_hypotheses_registers_ideator_result_unchanged() -> None:
    profile = DataProfile(row_count=25, col_count=4, task_type_hint="classification")
    tree = ResearchTree()
    ideator = RecordingIdeator()

    hypotheses = await generate_hypotheses(
        profile,
        [],
        [],
        tree,
        ideator=ideator,
    )

    assert ideator.calls == [(profile, [], [], tree)]
    assert hypotheses == ideator.result.hypotheses
    assert tree.pending_hypotheses() == ideator.result.hypotheses


@pytest.mark.asyncio
async def test_search_without_pending_hypotheses_requires_ideator(monkeypatch) -> None:
    async def fail_if_retrieval_starts(*_args, **_kwargs):
        raise AssertionError("retrieval started before Ideator validation")

    monkeypatch.setattr(
        "athena.workflows.search.search_loop.PaperSearch.search",
        fail_if_retrieval_starts,
    )
    loop = SearchLoop(
        ResearchTree(),
        _Workspace(),
        EvalSpec(
            primary=MetricDef(
                name="f1_macro", direction="maximize", description="Macro F1"
            )
        ),
        BudgetSnapshot(remaining=1),
        data_profile=TEST_PROFILE,
    )

    with pytest.raises(RuntimeError, match="^SEARCH requires an Ideator$"):
        await loop._pending_hypotheses()


@pytest.mark.asyncio
async def test_search_ideator_receives_constructor_data_profile(monkeypatch) -> None:
    async def no_papers(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "athena.workflows.search.search_loop.PaperSearch.search",
        no_papers,
    )
    profile = DataProfile(row_count=37, col_count=9, task_type_hint="regression")
    ideator = RecordingIdeator()
    tree = ResearchTree()
    loop = SearchLoop(
        tree,
        _Workspace(),
        EvalSpec(
            primary=MetricDef(
                name="rmse", direction="minimize", description="Root mean square error"
            )
        ),
        BudgetSnapshot(remaining=1),
        data_profile=profile,
        ideator=ideator,
    )

    pending = await loop._pending_hypotheses()

    assert len(ideator.calls) == 1
    assert ideator.calls[0][0] is profile
    assert ideator.calls[0][1:] == ([], [], tree)
    assert pending == ideator.result.hypotheses


@pytest.mark.asyncio
async def test_search_requires_a_successful_baseline() -> None:
    loop = SearchLoop(
        ResearchTree(),
        _Workspace(),
        EvalSpec(
            primary=MetricDef(
                name="f1_macro", direction="maximize", description="Macro F1"
            )
        ),
        BudgetSnapshot(remaining=1),
        data_profile=TEST_PROFILE,
    )

    with pytest.raises(RuntimeError, match="successful baseline"):
        await loop.run()


@pytest.mark.asyncio
async def test_search_records_candidate_lifecycle_and_accepts_new_sota() -> None:
    tree, baseline_id, hypothesis_id = _search_tree()
    loop, workspace, ranker, proximity, comparator, supervisor, agent, budget = _loop(
        tree
    )

    results = await loop.run()

    candidate_id = agent.observed_id
    assert candidate_id is not None
    candidate = tree.get_experiment(candidate_id)
    assert candidate.parent_id == baseline_id
    assert candidate.hypothesis_id == hypothesis_id
    assert candidate.status is ExperimentStatus.SUCCEEDED
    assert candidate.eval == results[0].eval
    assert candidate.verdict == ComparisonVerdict(winner="candidate", p_value=0.01)
    assert candidate.artifacts == {
        "diff": results[0].diff,
        "logs": results[0].logs,
    }
    assert tree.best_experiment_id() == candidate_id
    assert tree.get_hypothesis(hypothesis_id).status == "SUPPORTED"
    assert workspace.created == [(COMMIT, f"exp/{candidate_id}")]
    assert comparator.calls[0][2] == "maximize"
    assert len(supervisor.calls) == 1
    assert len(ranker.updates) == 1
    assert proximity.added == [hypothesis_id]
    assert budget.remaining == 0


@pytest.mark.asyncio
async def test_search_rejection_preserves_sota() -> None:
    tree, baseline_id, hypothesis_id = _search_tree()
    loop, _, ranker, _, _, _, agent, _ = _loop(
        tree,
        verdict=ComparisonVerdict(winner="baseline", p_value=0.01),
        action="REJECT",
    )

    await loop.run()

    assert tree.best_experiment_id() == baseline_id
    assert tree.get_experiment(agent.observed_id).verdict.winner == "baseline"
    assert tree.get_hypothesis(hypothesis_id).status == "REFUTED"
    assert len(ranker.updates) == 1


@pytest.mark.asyncio
async def test_search_execution_failure_records_logs_without_consuming_budget() -> None:
    tree, baseline_id, _ = _search_tree()
    error = CodeExecutionError("candidate crashed", logs="artifact://logs/crash")
    loop, _, ranker, _, _, _, agent, budget = _loop(tree, error=error)

    with pytest.raises(CodeExecutionError, match="candidate crashed"):
        await loop.run()

    candidate = tree.get_experiment(agent.observed_id)
    assert candidate.status is ExperimentStatus.FAILED
    assert candidate.error == "candidate crashed"
    assert candidate.artifacts == {"logs": "artifact://logs/crash"}
    assert tree.best_experiment_id() == baseline_id
    assert ranker.updates == []
    assert budget.remaining == 1


@pytest.mark.asyncio
async def test_search_cancellation_records_cancelled_without_decision() -> None:
    tree, baseline_id, _ = _search_tree()
    loop, _, ranker, _, _, supervisor, agent, budget = _loop(
        tree, error=asyncio.CancelledError()
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    assert tree.get_experiment(agent.observed_id).status is ExperimentStatus.CANCELLED
    assert tree.best_experiment_id() == baseline_id
    assert ranker.updates == []
    assert supervisor.calls == []
    assert budget.remaining == 1


def test_code_router_routes_small_change():
    h = Hypothesis(
        statement="test", intervention="add dropout", expected_effect="+0.01"
    )
    router = CodeRouter()
    assert router.route(h) == "qoder"


def test_code_router_routes_large_change():
    h = Hypothesis(
        statement="test",
        intervention="build an entirely new transformer architecture from scratch",
        expected_effect="+0.05",
    )
    router = CodeRouter()
    assert router.route(h) == "codex"


def test_supervisor_stops_on_streak():
    s = Supervisor()
    budget = BudgetSnapshot(
        remaining=10, no_improve_streak=4, max_no_improve=5, is_exhausted=False
    )
    decision = s.decide(ComparisonVerdict(winner="baseline", p_value=0.5), budget)
    assert decision.action == "STOP"


def test_supervisor_accepts_improvement():
    s = Supervisor()
    budget = BudgetSnapshot(remaining=5, max_no_improve=5)
    decision = s.decide(ComparisonVerdict(winner="candidate", p_value=0.01), budget)
    assert decision.action == "ACCEPT"


@pytest.mark.asyncio
async def test_code_agent_uses_a_fixed_entrypoint_without_fixing_model_filename(
    tmp_path,
    monkeypatch,
) -> None:
    scripts: list[str] = []

    async def fake_run(script: str, cwd, timeout_s: float) -> ProcessResult:
        scripts.append(script)
        if script == code_agent_module.EVALUATION_ENTRYPOINT:
            (cwd / "predictions.csv").write_text("prediction\n0\n", encoding="utf-8")
            (cwd / "eval_result.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp-test",
                        "primary": 0.5,
                        "secondary": {},
                    }
                ),
                encoding="utf-8",
            )
        return ProcessResult(returncode=0, output="ok")

    monkeypatch.setattr(code_agent_module, "_run_python", fake_run)
    hypothesis = Hypothesis(
        id="hyp-test",
        statement="Use a baseline",
        intervention="Train one deterministic model",
        expected_effect="Establish the reference metric",
        sources=["common_knowledge"],
    )
    spec = EvalSpec(
        primary=MetricDef(
            name="f1_macro",
            direction="maximize",
            description="Macro-averaged F1.",
        )
    )
    worktree = GitWorkBranch(
        path=str(tmp_path),
        branch="exp/test",
        base_commit="a" * 40,
    )

    result = await CodeAgent().execute(
        "exp-test",
        hypothesis,
        experiment_plan(),
        "a" * 40,
        spec,
        worktree,
    )

    assert result.experiment_id == "exp-test"
    assert result.eval.experiment_id == "exp-test"
    assert result.diff.startswith("artifact://")
    assert result.logs.startswith("artifact://")
    assert scripts == [
        code_agent_module.EXPERIMENT_ENTRYPOINT,
        code_agent_module.EVALUATION_ENTRYPOINT,
    ]
    assert (tmp_path / code_agent_module.EXPERIMENT_ENTRYPOINT).is_file()
    assert not (tmp_path / "model.py").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process_result", "expected"),
    [
        (ProcessResult(returncode=2, output="training crashed"), "exited with code 2"),
        (ProcessResult(returncode=-1, output="Timed out after 300s"), "Timed out"),
    ],
)
async def test_code_agent_rejects_process_failures_with_log_evidence(
    tmp_path,
    monkeypatch,
    process_result: ProcessResult,
    expected: str,
) -> None:
    async def fake_run(script: str, cwd, timeout_s: float) -> ProcessResult:
        return process_result

    monkeypatch.setattr(code_agent_module, "_run_python", fake_run)
    hypothesis = Hypothesis(
        id="hyp-test",
        statement="Use a baseline",
        intervention="Train one deterministic model",
        expected_effect="Establish the reference metric",
    )
    spec = EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="Macro F1")
    )
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    with pytest.raises(CodeExecutionError, match=expected) as captured:
        await CodeAgent().execute(
            "exp-test",
            hypothesis,
            experiment_plan(),
            "a" * 40,
            spec,
            worktree,
        )

    log_text = Path(captured.value.logs.removeprefix("artifact://")).read_text(
        encoding="utf-8"
    )
    assert "training" in log_text or "Timed out" in log_text


@pytest.mark.asyncio
async def test_code_agent_rejects_missing_evaluation_output(
    tmp_path, monkeypatch
) -> None:
    async def fake_run(script: str, cwd, timeout_s: float) -> ProcessResult:
        return ProcessResult(returncode=0, output="ok")

    monkeypatch.setattr(code_agent_module, "_run_python", fake_run)
    hypothesis = Hypothesis(
        id="hyp-test",
        statement="Use a baseline",
        intervention="Train one deterministic model",
        expected_effect="Establish the reference metric",
    )
    spec = EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="Macro F1")
    )
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    with pytest.raises(CodeExecutionError, match="did not produce eval_result.json"):
        await CodeAgent().execute(
            "exp-test",
            hypothesis,
            experiment_plan(),
            "a" * 40,
            spec,
            worktree,
        )

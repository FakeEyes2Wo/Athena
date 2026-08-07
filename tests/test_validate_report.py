"""VALIDATE and REPORT v2 lifecycle and evidence tests."""

import asyncio
from pathlib import Path

import pytest

from athena.core.workspace import GitWorkBranch
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.evaluation.factory import create_eval_spec
from athena.evaluation.types import (
    EvalResult,
    EvalSpec,
    EvaluationInputs,
    MetricDef,
)
from athena.workflows.report import ReportNarrative, Reporter
from athena.workflows.search.code_agent import CodeExecutionError, CodegenResult
from athena.workflows.validate import ValidationResult, Validator

COMMIT = "b" * 40


def _plan(kind: str, change: str) -> ExperimentPlan:
    return ExperimentPlan(
        kind=kind,
        change=change,
        run_config_ref=f"artifact://runs/{kind}/config",
        budget={"max_trials": 1},
        acceptance_rule="The frozen evaluator completes",
        rubrics=["Uses frozen evidence"],
    )


def _successful_experiment(
    tree: ResearchTree,
    experiment_id: str,
    hypothesis_id: str,
    *,
    kind: str,
    parent_id: str | None,
    primary: float,
    commit: str = COMMIT,
) -> None:
    tree.add_experiment(
        experiment_id,
        Experiment(
            parent_id=parent_id,
            hypothesis_id=hypothesis_id,
            commit=commit,
            plan=_plan(kind, f"Execute {kind}"),
            gitwork=GitWorkBranch(
                path=f"C:/worktrees/{experiment_id}",
                branch=f"{kind}/{experiment_id}",
                base_commit=commit,
            ),
        ),
    )
    tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)
    tree.complete_experiment(
        experiment_id,
        eval=EvalResult(
            experiment_id=experiment_id,
            primary=primary,
            per_sample=f"artifact://samples/{experiment_id}",
        ),
        verdict=None,
        artifacts={
            "diff": f"artifact://diffs/{experiment_id}",
            "logs": f"artifact://logs/{experiment_id}",
        },
    )


def _tree() -> tuple[ResearchTree, str]:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="hyp-baseline",
            statement="Establish a baseline",
            intervention="Train the default model",
            expected_effect="Provide the reference score",
            status="SUPPORTED",
        )
    )
    tree.add_hypothesis(
        Hypothesis(
            id="hyp-search",
            parent_id="exp-baseline",
            statement="Add calibrated features",
            intervention="Add calibrated aggregate features",
            expected_effect="Improve macro F1",
            status="SUPPORTED",
        )
    )
    _successful_experiment(
        tree,
        "exp-baseline",
        "hyp-baseline",
        kind="baseline",
        parent_id=None,
        primary=0.7,
    )
    _successful_experiment(
        tree,
        "exp-sota",
        "hyp-search",
        kind="search",
        parent_id="exp-baseline",
        primary=0.8,
    )
    tree.set_sota("exp-sota")
    return tree, "exp-sota"


def _inputs(root: Path, phase: str = "validation") -> EvaluationInputs:
    """Host-owned input set; paths need not exist for the recording agents."""
    return EvaluationInputs(
        phase=phase,
        train_path=root / "train.csv",
        features_path=root / "features.csv",
        labels_path=root / "labels.csv",
        target="label",
    )


def _dummy_result(
    experiment_id: str = "exp-x", commit: str = "c" * 40
) -> CodegenResult:
    return CodegenResult(
        experiment_id=experiment_id,
        commit=commit,
        diff=f"sha256:{'0' * 64}",
        eval=EvalResult(
            experiment_id=experiment_id,
            primary=0.8,
            per_sample=f"sha256:{'1' * 64}",
        ),
        evaluation=f"sha256:{'2' * 64}",
        logs=f"sha256:{'3' * 64}",
    )


def _tree_with_sota(validation_inputs) -> tuple[ResearchTree, str]:
    """A tree with a successful SEARCH SOTA (commit 'c'*40) and two hypotheses."""
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="hyp-baseline",
            statement="Establish a baseline",
            intervention="Train the default model",
            expected_effect="Provide the reference score",
            status="SUPPORTED",
        )
    )
    tree.add_hypothesis(
        Hypothesis(
            id="hyp-search",
            parent_id="exp-baseline",
            statement="Add calibrated features",
            intervention="Add calibrated aggregate features",
            expected_effect="Improve macro F1",
            status="SUPPORTED",
        )
    )
    _successful_experiment(
        tree,
        "exp-baseline",
        "hyp-baseline",
        kind="baseline",
        parent_id=None,
        primary=0.7,
    )
    _successful_experiment(
        tree,
        "exp-sota",
        "hyp-search",
        kind="search",
        parent_id="exp-baseline",
        primary=0.8,
        commit="c" * 40,
    )
    tree.set_sota("exp-sota")
    return tree, "exp-sota"


class RecordingTrustedAgent:
    def __init__(self, *, commit: str) -> None:
        self.commit = commit
        self.generated: list[str] = []
        self.frozen: list[str] = []

    async def execute(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs,
    ):
        self.generated.append(inputs.phase)
        return _dummy_result(experiment_id=experiment_id, commit=self.commit)

    async def execute_frozen(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs,
    ):
        self.frozen.append(inputs.phase)
        return _dummy_result(experiment_id=experiment_id, commit=parent_commit)


class RecordingWorkspace:
    def __init__(self) -> None:
        self.created: list[GitWorkBranch] = []

    async def create(self, base_commit: str, branch: str) -> GitWorkBranch:
        worktree = GitWorkBranch(
            path=f"C:/worktrees/{branch.replace('/', '-')}",
            branch=branch,
            base_commit=base_commit,
        )
        self.created.append(worktree)
        return worktree


class RecordingCodeAgent:
    def __init__(
        self,
        tree: ResearchTree,
        *,
        fail_kind: str | None = None,
        cancel_kind: str | None = None,
    ) -> None:
        self.tree = tree
        self.fail_kind = fail_kind
        self.cancel_kind = cancel_kind
        self.calls: list[tuple[str, str, ExperimentPlan, GitWorkBranch]] = []
        self.phases: list[str] = []

    async def execute(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs,
    ) -> CodegenResult:
        assert (
            self.tree.get_experiment(experiment_id).status is ExperimentStatus.RUNNING
        )
        self.calls.append((experiment_id, hypothesis.id, plan, worktree))
        self.phases.append(inputs.phase)
        if plan.kind == self.cancel_kind:
            raise asyncio.CancelledError()
        if plan.kind == self.fail_kind:
            raise CodeExecutionError(
                f"{plan.kind} failed", logs=f"artifact://logs/{experiment_id}"
            )
        primary = 0.77 if plan.kind == "final-test" else 0.65
        return CodegenResult(
            experiment_id=experiment_id,
            commit=parent_commit,
            diff=f"artifact://diffs/{experiment_id}",
            evaluation=f"artifact://evaluations/{experiment_id}",
            logs=f"artifact://logs/{experiment_id}",
            eval=EvalResult(
                experiment_id=experiment_id,
                primary=primary,
                per_sample=f"artifact://samples/{experiment_id}",
            ),
        )

    async def execute_frozen(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs,
    ) -> CodegenResult:
        assert (
            self.tree.get_experiment(experiment_id).status is ExperimentStatus.RUNNING
        )
        self.calls.append((experiment_id, hypothesis.id, plan, worktree))
        self.phases.append(inputs.phase)
        return CodegenResult(
            experiment_id=experiment_id,
            commit=parent_commit,
            diff=f"artifact://diffs/{experiment_id}",
            evaluation=f"artifact://evaluations/{experiment_id}",
            logs=f"artifact://logs/{experiment_id}",
            eval=EvalResult(
                experiment_id=experiment_id,
                primary=0.77,
                per_sample=f"artifact://samples/{experiment_id}",
            ),
        )


def _validator(tree: ResearchTree, **agent_options):
    workspace = RecordingWorkspace()
    agent = RecordingCodeAgent(tree, **agent_options)
    validator = Validator(
        workspace,
        agent,
        EvalSpec(
            primary=MetricDef(
                name="f1_macro", direction="maximize", description="Macro F1"
            )
        ),
        validation_inputs=_inputs(Path("C:/validation")),
        test_inputs=_inputs(Path("C:/test"), phase="test"),
    )
    return validator, workspace, agent


@pytest.mark.asyncio
async def test_validator_records_isolated_ablations_and_final_test() -> None:
    tree, sota_id = _tree()
    validator, workspace, agent = _validator(tree)

    result = await validator.run(sota_id, tree)

    assert result.sota_id == sota_id
    assert len(result.ablation_ids) == len(tree.hypotheses_path(sota_id))
    assert len({worktree.branch for worktree in workspace.created}) == 3
    for experiment_id in result.ablation_ids:
        experiment = tree.get_experiment(experiment_id)
        assert experiment.parent_id == sota_id
        assert experiment.plan.kind == "ablation"
        assert experiment.plan.change == (
            f"Remove intervention for {experiment.hypothesis_id}"
        )
        assert experiment.status is ExperimentStatus.SUCCEEDED
        assert experiment.eval is not None
        assert experiment.artifacts.keys() >= {"diff", "logs"}
    final_test = tree.get_experiment(result.final_test_id)
    assert final_test.parent_id == sota_id
    assert final_test.plan.kind == "final-test"
    assert final_test.status is ExperimentStatus.SUCCEEDED
    assert final_test.eval is not None
    assert final_test.verdict is None
    assert tree.best_experiment_id() == sota_id
    assert [call[1] for call in agent.calls[:2]] == [
        "hyp-baseline",
        "hyp-search",
    ]


@pytest.mark.asyncio
async def test_validator_is_idempotent_after_complete_validation() -> None:
    tree, sota_id = _tree()
    validator, workspace, agent = _validator(tree)
    first = await validator.run(sota_id, tree)
    call_count = len(agent.calls)

    second = await validator.run(sota_id, tree)

    assert second == first
    assert len(agent.calls) == call_count
    assert len(workspace.created) == call_count


@pytest.mark.asyncio
async def test_validator_rejects_wrong_or_missing_sota() -> None:
    tree, sota_id = _tree()
    validator, _, _ = _validator(tree)

    with pytest.raises(RuntimeError, match="selected SOTA"):
        await validator.run("exp-baseline", tree)
    with pytest.raises(KeyError, match="unknown experiment"):
        await validator.run("exp-missing", tree)
    assert tree.best_experiment_id() == sota_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_options", "expected_status", "error_type"),
    [
        ({"fail_kind": "ablation"}, ExperimentStatus.FAILED, CodeExecutionError),
        (
            {"cancel_kind": "ablation"},
            ExperimentStatus.CANCELLED,
            asyncio.CancelledError,
        ),
    ],
)
async def test_validator_preserves_failed_or_cancelled_ablation(
    agent_options, expected_status, error_type
) -> None:
    tree, sota_id = _tree()
    validator, _, agent = _validator(tree, **agent_options)

    with pytest.raises(error_type):
        await validator.run(sota_id, tree)

    experiment = tree.get_experiment(agent.calls[0][0])
    assert experiment.status is expected_status
    if expected_status is ExperimentStatus.FAILED:
        assert experiment.error == "ablation failed"
        assert "logs" in experiment.artifacts


@pytest.mark.asyncio
async def test_validator_runs_ablation_on_validation_and_final_test_frozen(
    tmp_path: Path,
) -> None:
    from athena.workflows.validate.ablation import Validator

    agent = RecordingTrustedAgent(commit="c" * 40)
    eval_spec = create_eval_spec("classification")
    validation_inputs = _inputs(tmp_path / "v")
    test_inputs = _inputs(tmp_path / "t", phase="test")
    validator = Validator(
        None,
        agent,
        eval_spec,
        validation_inputs=validation_inputs,
        test_inputs=test_inputs,
    )
    tree, sota_id = _tree_with_sota(validation_inputs)
    result = await validator.run(sota_id, tree)

    assert agent.generated == ["validation", "validation"]
    assert agent.frozen == ["test"]
    assert tree.get_experiment(result.final_test_id).commit == "c" * 40


@pytest.mark.asyncio
async def test_reporter_uses_complete_tree_evidence_and_attaches_artifact(
    tmp_path: Path,
) -> None:
    tree, sota_id = _tree()
    validator, _, _ = _validator(tree)
    validation = await validator.run(sota_id, tree)

    first_ref = await Reporter(output_dir=tmp_path).generate(sota_id, tree)
    first = Path(first_ref.removeprefix("artifact://")).read_bytes()
    second_ref = await Reporter(output_dir=tmp_path).generate(sota_id, tree)
    second = Path(second_ref.removeprefix("artifact://")).read_bytes()

    final = tree.get_experiment(validation.final_test_id)
    report = first.decode("utf-8")
    assert tree.get_experiment(sota_id).artifacts["report"] == first_ref
    assert f"Final primary: {final.eval.primary:.4f}" in report
    assert "Ablation" in report
    assert first_ref == second_ref
    assert first == second


@pytest.mark.asyncio
async def test_reporter_combines_narrative_with_deterministic_metrics(
    tmp_path: Path,
) -> None:
    class Agent:
        async def run(self, prompt: str, *, output_type):
            assert output_type is ReportNarrative
            assert '"final_test": {"experiment_id":' in prompt
            assert '"primary": 0.77' in prompt
            return ReportNarrative(
                executive_summary="Evidence supports the selected experiment.",
                limitations=["One final-test run was used."],
                recommendations=["Repeat with additional seeds."],
            )

    tree, sota_id = _tree()
    validator, _, _ = _validator(tree)
    await validator.run(sota_id, tree)

    ref = await Reporter(agent=Agent(), output_dir=tmp_path).generate(sota_id, tree)
    report = Path(ref.removeprefix("artifact://")).read_text(encoding="utf-8")

    assert "Evidence supports the selected experiment." in report
    assert "One final-test run was used." in report
    assert "Repeat with additional seeds." in report


@pytest.mark.asyncio
async def test_reporter_rejects_incomplete_or_wrong_validation() -> None:
    tree, sota_id = _tree()
    reporter = Reporter()

    with pytest.raises(RuntimeError, match="complete validation"):
        await reporter.generate(sota_id, tree)
    with pytest.raises(RuntimeError, match="selected SOTA"):
        await reporter.generate("exp-baseline", tree)


def test_compatibility_workflow_exports_are_canonical() -> None:
    from athena.experiment.report import Reporter as CompatibilityReporter
    from athena.experiment.validate import Validator as CompatibilityValidator
    from athena.workflows.report import Reporter as CanonicalReporter
    from athena.workflows.validate import Validator as CanonicalValidator

    assert CompatibilityValidator is CanonicalValidator
    assert CompatibilityReporter is CanonicalReporter

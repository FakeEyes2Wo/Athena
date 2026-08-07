import json
import os
from pathlib import Path

import pytest

from athena.code.backends.base import CodeBackend
from athena.code.types import GenerationResult
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.workspace import GitWorkBranch
from athena.evaluation.factory import create_eval_spec
from athena.evaluation.trusted import TrustedEvaluator
from athena.evaluation.types import EvaluationInputs, EvalSpec, MetricDef
from athena.storage.artifact_store import LocalArtifactStore
from athena.workflows.search import code_agent as code_agent_module
from athena.workflows.search.code_agent import (
    CodeAgent,
    CodeExecutionError,
)


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="hyp-test",
        statement="Use a deterministic classifier",
        intervention="Create the requested experiment entrypoint",
        expected_effect="Produce a finite frozen metric",
    )


def _plan() -> ExperimentPlan:
    return ExperimentPlan(
        kind="search",
        change="Create the requested experiment entrypoint",
        run_config_ref="artifact://runs/test/config",
        budget={"max_trials": 1},
        acceptance_rule="The frozen evaluator completes",
        rubrics=["Only run_experiment.py changes"],
    )


def _spec() -> EvalSpec:
    return EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="Macro F1")
    )


def _inputs(tmp_path) -> EvaluationInputs:
    train = tmp_path / "train.csv"
    features = tmp_path / "features.csv"
    labels = tmp_path / "labels.csv"
    train.write_text("__athena_row_id,label\n0,0\n1,1\n", encoding="utf-8")
    features.write_text("__athena_row_id,feature\n0,1\n1,2\n", encoding="utf-8")
    labels.write_text("__athena_row_id,label\n0,0\n1,1\n", encoding="utf-8")
    return EvaluationInputs(
        phase="validation",
        train_path=train,
        features_path=features,
        labels_path=labels,
        target="label",
    )


class RecordingWorkspace:
    def __init__(self) -> None:
        self.diffed = []
        self.committed = []

    async def diff(self, worktree):
        self.diffed.append(worktree)
        return "artifact://diffs/approved"

    async def commit(self, worktree, approved_diff_ref, message):
        self.committed.append((worktree, approved_diff_ref, message))
        return "c" * 40


class WritingBackend(CodeBackend):
    def __init__(self, *, protect_eval: bool = False, fail_first: bool = False):
        self.protect_eval = protect_eval
        self.fail_first = fail_first
        self.calls = 0
        self.feedback = []

    async def generate(self, prompt, target_dir, previous_outputs, history):
        self.calls += 1
        self.feedback.append(list(previous_outputs))
        root = Path(target_dir)
        if self.protect_eval:
            (root / "eval.py").write_text("tampered\n", encoding="utf-8")
        if self.fail_first and self.calls == 1:
            source = "raise RuntimeError('first round failed')\n"
        else:
            source = (
                "from pathlib import Path\n"
                "Path('predictions.csv').write_text("
                "'__athena_row_id,prediction\\n0,0\\n1,1\\n', encoding='utf-8')\n"
            )
        (root / "run_experiment.py").write_text(source, encoding="utf-8")
        return GenerationResult(
            files_created=["run_experiment.py"] if self.calls == 1 else [],
            files_modified=["run_experiment.py"] if self.calls > 1 else [],
        )


class EvalFailFirstBackend(CodeBackend):
    """Round 1 runs but produces un-scorable predictions; round 2 succeeds."""

    def __init__(self):
        self.calls = 0
        self.feedback = []

    async def generate(self, prompt, target_dir, previous_outputs, history):
        self.calls += 1
        self.feedback.append(list(previous_outputs))
        root = Path(target_dir)
        if self.calls == 1:
            source = (
                "from pathlib import Path\n"
                "Path('predictions.csv').write_text("
                "'__athena_row_id,badcol\\n0,0\\n1,1\\n', encoding='utf-8')\n"
            )
        else:
            source = (
                "from pathlib import Path\n"
                "Path('predictions.csv').write_text("
                "'__athena_row_id,prediction\\n0,0\\n1,1\\n', encoding='utf-8')\n"
            )
        (root / "run_experiment.py").write_text(source, encoding="utf-8")
        return GenerationResult(
            files_created=["run_experiment.py"] if self.calls == 1 else [],
            files_modified=["run_experiment.py"] if self.calls > 1 else [],
        )


def _injected_agent(
    backend: CodeBackend,
    tmp_path,
    *,
    workspace=None,
    max_rounds: int = 3,
) -> CodeAgent:
    artifacts = LocalArtifactStore(tmp_path / "store")
    return CodeAgent(
        backend="qoder",
        backends={"qoder": backend},
        workspace=workspace,
        artifacts=artifacts,
        evaluator=TrustedEvaluator(artifacts),
        max_rounds=max_rounds,
    )


@pytest.mark.asyncio
async def test_code_agent_stages_phase_manifest_and_inputs(tmp_path) -> None:
    inputs = _inputs(tmp_path)
    worktree = GitWorkBranch(path=str(tmp_path), branch="exp/t", base_commit="a" * 40)
    agent = CodeAgent()
    await agent._stage_inputs(tmp_path, inputs)
    manifest = json.loads((tmp_path / ".athena" / "phase_manifest.json").read_text())
    assert manifest["phase"] == "validation"
    assert manifest["row_id_column"] == "__athena_row_id"
    assert manifest["target"] == "label"
    assert (tmp_path / ".athena" / "inputs" / "train.csv").is_file()
    assert "labels" not in json.dumps(manifest)


@pytest.mark.asyncio
async def test_code_agent_runs_the_frozen_evaluator_source(tmp_path) -> None:
    spec = create_eval_spec("classification")
    inputs = _inputs(tmp_path)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    result = await _injected_agent(WritingBackend(), tmp_path).execute(
        "exp-test", _hypothesis(), _plan(), "a" * 40, spec, worktree, inputs=inputs
    )

    assert (tmp_path / "eval.py").read_text(encoding="utf-8") == spec.eval_script
    assert result.eval.experiment_id == "exp-test"
    assert result.eval.primary == 1.0


@pytest.mark.asyncio
async def test_code_agent_generates_reviews_and_commits_real_diff(tmp_path) -> None:
    backend = WritingBackend()
    workspace = RecordingWorkspace()
    inputs = _inputs(tmp_path)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    result = await _injected_agent(backend, tmp_path, workspace=workspace).execute(
        "exp-test", _hypothesis(), _plan(), "a" * 40, _spec(), worktree, inputs=inputs
    )

    assert backend.calls == 1
    assert workspace.diffed == [worktree]
    assert workspace.committed[0][1] == "artifact://diffs/approved"
    assert result.diff == "artifact://diffs/approved"
    assert result.commit == "c" * 40
    assert result.eval.primary == 1.0


@pytest.mark.asyncio
async def test_code_agent_rejects_backend_changes_to_frozen_evaluator(tmp_path) -> None:
    backend = WritingBackend(protect_eval=True)
    inputs = _inputs(tmp_path)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    with pytest.raises(CodeExecutionError, match="protected file changed"):
        await _injected_agent(backend, tmp_path).execute(
            "exp-test",
            _hypothesis(),
            _plan(),
            "a" * 40,
            _spec(),
            worktree,
            inputs=inputs,
        )


@pytest.mark.asyncio
async def test_code_agent_feeds_execution_failure_to_revision_round(
    tmp_path,
) -> None:
    backend = WritingBackend(fail_first=True)
    workspace = RecordingWorkspace()
    inputs = _inputs(tmp_path)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    result = await _injected_agent(
        backend, tmp_path, workspace=workspace, max_rounds=2
    ).execute(
        "exp-test", _hypothesis(), _plan(), "a" * 40, _spec(), worktree, inputs=inputs
    )

    assert result.eval.primary == 1.0
    assert backend.calls == 2
    assert backend.feedback[1]
    assert "first round failed" in backend.feedback[1][0].stderr


@pytest.mark.asyncio
async def test_code_agent_only_feeds_failed_rounds_to_revision_prompt(
    tmp_path,
) -> None:
    backend = EvalFailFirstBackend()
    inputs = _inputs(tmp_path)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    result = await _injected_agent(backend, tmp_path, max_rounds=2).execute(
        "exp-test", _hypothesis(), _plan(), "a" * 40, _spec(), worktree, inputs=inputs
    )

    assert backend.calls == 2
    assert result.eval.primary == 1.0
    fed = backend.feedback[1]
    assert len(fed) == 1
    assert fed[0].returncode == -1
    assert "trusted evaluation failed" in fed[0].stderr


@pytest.mark.asyncio
async def test_generated_tree_policy_permits_auxiliary_files(tmp_path):
    from athena.code.file_policy import GeneratedTreePolicy

    (tmp_path / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "stack.py").write_text("x", encoding="utf-8")
    GeneratedTreePolicy().validate(
        tmp_path, {"run_experiment.py", "models/stack.py", "config.json"}
    )


@pytest.mark.asyncio
async def test_generated_tree_policy_rejects_protected_and_missing_entrypoint(
    tmp_path,
):
    from athena.code.file_policy import GeneratedTreePolicy
    from athena.workflows.search.code_agent import CodeExecutionError

    (tmp_path / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    policy = GeneratedTreePolicy()
    with pytest.raises(CodeExecutionError, match="protected path changed"):
        policy.validate(tmp_path, {"run_experiment.py", "eval.py"})
    (tmp_path / "run_experiment.py").unlink()
    with pytest.raises(CodeExecutionError, match="run_experiment.py"):
        policy.validate(tmp_path, set())


@pytest.mark.asyncio
async def test_generated_tree_policy_rejects_external_symlink(tmp_path):
    from athena.code.file_policy import GeneratedTreePolicy
    from athena.workflows.search.code_agent import CodeExecutionError

    (tmp_path / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "leak.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires platform privileges")
    with pytest.raises(CodeExecutionError, match="symlink"):
        GeneratedTreePolicy().validate(tmp_path, {"leak.py"})

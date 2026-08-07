import json
from pathlib import Path

import pytest

from athena.code.backends.base import CodeBackend
from athena.code.types import GenerationResult
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.workspace import GitWorkBranch
from athena.evaluation.types import EvalSpec, MetricDef
from athena.evaluation.factory import create_eval_spec
from athena.workflows.search import code_agent as code_agent_module
from athena.workflows.search.code_agent import (
    CodeAgent,
    CodeExecutionError,
    ProcessResult,
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
        source = (
            "raise RuntimeError('first round failed')\n"
            if self.fail_first and self.calls == 1
            else "print('generated experiment')\n"
        )
        (root / "run_experiment.py").write_text(source, encoding="utf-8")
        return GenerationResult(
            files_created=["run_experiment.py"] if self.calls == 1 else [],
            files_modified=["run_experiment.py"] if self.calls > 1 else [],
        )


async def _fake_evaluation_run(script, cwd, timeout_s, env=None):
    if script == "run_experiment.py":
        (cwd / "predictions.csv").write_text("prediction\n0\n", encoding="utf-8")
        (cwd / "labels.csv").write_text("label\n0\n", encoding="utf-8")
    else:
        (cwd / "eval_result.json").write_text(
            json.dumps({"experiment_id": "exp-test", "primary": 1.0, "secondary": {}}),
            encoding="utf-8",
        )
    return ProcessResult(returncode=0, output="ok")


@pytest.mark.asyncio
async def test_code_agent_runs_the_frozen_evaluator_source(tmp_path) -> None:
    (tmp_path / "run_experiment.py").write_text(
        "from pathlib import Path\n"
        "Path('predictions.csv').write_text('prediction\\n0\\n1\\n', encoding='utf-8')\n"
        "Path('labels.csv').write_text('label\\n0\\n1\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    spec = create_eval_spec("classification")
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    result = await CodeAgent().execute(
        "exp-test", _hypothesis(), _plan(), "a" * 40, spec, worktree
    )

    assert (tmp_path / "eval.py").read_text(encoding="utf-8") == spec.eval_script
    assert result.eval.experiment_id == "exp-test"
    assert result.eval.primary == 1.0


@pytest.mark.asyncio
async def test_code_agent_generates_reviews_and_commits_real_diff(
    tmp_path, monkeypatch
) -> None:
    backend = WritingBackend()
    workspace = RecordingWorkspace()
    monkeypatch.setattr(code_agent_module, "_run_python", _fake_evaluation_run)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    result = await CodeAgent(
        backend="qoder", backends={"qoder": backend}, workspace=workspace
    ).execute("exp-test", _hypothesis(), _plan(), "a" * 40, _spec(), worktree)

    assert backend.calls == 1
    assert workspace.diffed == [worktree]
    assert workspace.committed[0][1] == "artifact://diffs/approved"
    assert result.diff == "artifact://diffs/approved"
    assert result.commit == "c" * 40
    assert result.eval.primary == 1.0


@pytest.mark.asyncio
async def test_code_agent_rejects_backend_changes_to_frozen_evaluator(tmp_path) -> None:
    backend = WritingBackend(protect_eval=True)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    with pytest.raises(CodeExecutionError, match="protected file changed"):
        await CodeAgent(backend="qoder", backends={"qoder": backend}).execute(
            "exp-test", _hypothesis(), _plan(), "a" * 40, _spec(), worktree
        )


@pytest.mark.asyncio
async def test_code_agent_feeds_execution_failure_to_revision_round(
    tmp_path, monkeypatch
) -> None:
    backend = WritingBackend(fail_first=True)
    workspace = RecordingWorkspace()
    monkeypatch.setattr(code_agent_module, "_run_python", _fake_evaluation_run)
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    result = await CodeAgent(
        backend="qoder",
        backends={"qoder": backend},
        workspace=workspace,
        max_rounds=2,
    ).execute("exp-test", _hypothesis(), _plan(), "a" * 40, _spec(), worktree)

    assert result.eval.primary == 1.0
    assert backend.calls == 2
    assert backend.feedback[1]
    assert "first round failed" in backend.feedback[1][0].stderr

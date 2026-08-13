"""End-to-end Plan manifest execution, trusted scoring, and Git commit."""

import json
import sys
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import BundleMetadata, DataScriptRunner
from athena.research.supervisor.experiment import PlanRunner, load_best
from athena.research.supervisor.plans import PlanInput, PlanState

_REF = "sha256:" + "a" * 64

EVAL_PY = """\
import csv, json, sys

def main():
    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')
    preds = {}
    with open('outputs/predictions.csv', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 2 and parts[0] != '__athena_row_id':
                preds[parts[0]] = float(parts[1])
    labels = {}
    with open('labels.csv', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 2 and parts[0] != '__athena_row_id':
                labels[parts[0]] = float(parts[1])
    rows = [(preds[k], labels[k]) for k in preds if k in labels]
    acc = sum(1.0 for a, b in rows if a == b) / len(rows)
    json.dump({'primary': acc, 'metric': 'accuracy'}, out)

main()
"""


async def _frozen_eval_bundle(tmp_path, store) -> str:
    """Build and freeze a stdlib-only accuracy evaluator over frozen labels."""
    runner = DataScriptRunner(store=store, workdir=tmp_path / "eval-work")
    draft = tmp_path / "eval-draft"
    draft.mkdir(exist_ok=True)
    (draft / "eval.py").write_text(EVAL_PY, encoding="utf-8")
    (draft / "labels.csv").write_text(
        "__athena_row_id,label\nrow_1,1\nrow_2,0\n", encoding="utf-8"
    )
    (draft / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'eval'\n"
        "version = '0.1.0'\n"
        "requires-python = '>=3.11'\n"
        "dependencies = []\n",
        encoding="utf-8",
    )
    bundle = await runner.freeze(draft, BundleMetadata(entrypoint="eval.py"))
    return await store.put_text(bundle.model_dump_json())


@pytest.mark.asyncio
async def test_plan_turn_executes_manifest_scores_and_commits(
    tmp_path, monkeypatch
) -> None:
    for name, value in {
        "GIT_AUTHOR_NAME": "Athena Test",
        "GIT_AUTHOR_EMAIL": "athena@example.invalid",
        "GIT_COMMITTER_NAME": "Athena Test",
        "GIT_COMMITTER_EMAIL": "athena@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)

    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _frozen_eval_bundle(tmp_path, store)
    plan_input = PlanInput(evaluator_ref=evaluator_ref, tree_ref=_REF)

    workspace = LocalGitWorkspace(
        tmp_path / "repo",
        tmp_path / "worktrees",
        lambda content: store.put_bytes(content),
    )
    base = await workspace.init(repo_path=tmp_path / "repo")
    branch = await workspace.create(base, "athena/plan/h1")
    workdir = Path(branch.path)

    (workdir / "predict.py").write_text(
        "from pathlib import Path\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        "Path('outputs/predictions.csv').write_text("
        "'__athena_row_id,prediction\\nrow_1,1\\nrow_2,0\\n')\n",
        encoding="utf-8",
    )
    (workdir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [[sys.executable, "predict.py"]],
                "outputs": {
                    "predictions": "outputs/predictions.csv",
                    "report": "report.md",
                },
            }
        ),
        encoding="utf-8",
    )

    runner = PlanRunner(
        execution=ExecutionRuntime(
            project_root=tmp_path, environment_root=tmp_path, store=store
        ),
        store=store,
        evaluator=TrustedEvaluator(
            DataScriptRunner(store=store, workdir=tmp_path / "eval-run")
        ),
        workspace=workspace,
        branch=branch,
        context=ExecutionContext(
            project_root=tmp_path,
            workspace_root=workdir,
            environment_root=tmp_path,
        ),
        direction="maximize",
    )
    state = PlanState(
        kind="SEARCH", context_ref=_REF, turns_used=0, turn_limit=12, patience=3
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == "scored"
    assert result.metric == 1.0
    assert result.commit is not None
    assert result.next_state is not None
    assert result.next_state.stale_rounds == 0
    assert result.next_state.best_ref is not None
    best = await load_best(result.next_state.best_ref, store)
    assert best.metric == 1.0
    assert best.commit == result.commit

    predictions = await store.get_text(result.predictions_ref)
    assert "row_1,1" in predictions

    # the trusted revision is committed on the Plan branch
    assert (workdir / "predict.py").is_file()
    assert result.commit

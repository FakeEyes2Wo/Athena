"""A real AgentRuntime prompt receives a failed experiment's cause and repairs it."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.experiment import PlanRunner, load_best
from tests.integration.research.test_plan_optimization import _frozen_eval_bundle
from tests.integration.research.test_rolling_search import _eventually, _Harness


@pytest.mark.asyncio
async def test_search_agent_repairs_failed_script_then_scores_same_plan(
    tmp_path, monkeypatch
) -> None:
    for name, value in {
        "GIT_AUTHOR_NAME": "Athena Test",
        "GIT_AUTHOR_EMAIL": "athena@example.invalid",
        "GIT_COMMITTER_NAME": "Athena Test",
        "GIT_COMMITTER_EMAIL": "athena@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)

    harness = _Harness(tmp_path)
    await harness.initialize()
    try:
        harness.state.search_limit = 1
        harness.state.concurrency = 1
        evaluator_ref = await _frozen_eval_bundle(tmp_path, harness.store)
        harness.supervisor.evaluator_ref = evaluator_ref
        expected_features = tmp_path / "search_features.csv"
        expected_features.write_text(
            "__athena_row_id,feature\nrow_1,a\nrow_2,b\n", encoding="utf-8"
        )

        await harness.supervisor.start_plan("h1")
        branch = harness.supervisor.workspace("h1")
        workdir = Path(branch.path)
        plan_input = await harness.supervisor.plan_input("h1")
        runner = PlanRunner(
            execution=ExecutionRuntime(
                project_root=tmp_path,
                environment_root=tmp_path,
                store=harness.store,
            ),
            store=harness.store,
            evaluator=TrustedEvaluator(
                DataScriptRunner(store=harness.store, workdir=tmp_path / "eval-run")
            ),
            workspace=harness.git,
            branch=branch,
            context=ExecutionContext(
                project_root=tmp_path,
                workspace_root=workdir,
                environment_root=tmp_path,
                predict_features=expected_features,
            ),
        )

        async def execute_plan(plan_id, state):
            return await runner.run_turn(
                plan_id,
                state,
                await harness.supervisor.plan_input(plan_id),
            )

        harness.supervisor._deps.research = replace(
            harness.supervisor._deps.research, plan=execute_plan
        )
        (workdir / "model.py").write_text(
            "print('early output ' * 1000)\n"
            "raise RuntimeError('NEXT_PROMPT_ROOT_CAUSE')\n",
            encoding="utf-8",
        )
        (workdir / "experiment.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "commands": [[sys.executable, "model.py"]],
                    "outputs": {
                        "predictions": "outputs/predictions",
                        "report": "report.md",
                    },
                }
            ),
            encoding="utf-8",
        )

        harness.task = asyncio.create_task(harness.supervisor.run_search())
        await _eventually(lambda: len(harness.prompts["h1"]) == 1)
        first_context_ref = harness.state.plans["h1"].context_ref
        first_workspace = harness.supervisor.workspace_path("h1")
        harness.decisions["h1"].append("submit")
        harness.gates["h1"].set()

        await _eventually(lambda: len(harness.prompts["h1"]) == 2)
        repair_prompt = harness.prompts["h1"][1]
        assert "NEXT_PROMPT_ROOT_CAUSE" in repair_prompt
        assert harness.state.plans["h1"].context_ref == first_context_ref
        assert harness.supervisor.workspace_path("h1") == first_workspace

        (workdir / "model.py").write_text(
            "from pathlib import Path\n"
            "Path('outputs/predictions').mkdir(parents=True, exist_ok=True)\n"
            "Path('outputs/predictions/predictions.csv').write_text("
            "'__athena_row_id,prediction\\nrow_1,1\\nrow_2,0\\n')\n",
            encoding="utf-8",
        )
        harness.decisions["h1"].append("submit")
        harness.gates["h1"].set()

        await _eventually(lambda: "h1" not in harness.state.plans)
        if harness.task is not None:
            await asyncio.wait_for(harness.task, timeout=5)

        experiment = harness.tree.get_experiment("exp_h1")
        assert experiment.status.value == "SUCCEEDED"
        assert experiment.eval is not None
        assert experiment.eval.primary == 1.0
        assert harness.supervisor.workspace_path("h1") == first_workspace
        assert len(harness.prompts["h1"]) == 2
        assert plan_input.evaluator_ref == evaluator_ref
    finally:
        await harness.supervisor.stop()
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("module_entry", [False, True], ids=["script", "module"])
async def test_zero_patience_continues_after_score_and_submits_same_plan(
    tmp_path, monkeypatch, module_entry
) -> None:
    for name, value in {
        "GIT_AUTHOR_NAME": "Athena Test",
        "GIT_AUTHOR_EMAIL": "athena@example.invalid",
        "GIT_COMMITTER_NAME": "Athena Test",
        "GIT_COMMITTER_EMAIL": "athena@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)

    harness = _Harness(tmp_path)
    await harness.initialize()
    try:
        harness.state.search_limit = 1
        harness.state.concurrency = 1
        evaluator_ref = await _frozen_eval_bundle(tmp_path, harness.store)
        harness.supervisor.evaluator_ref = evaluator_ref
        await harness.supervisor.start_plan("h1")
        plan_state = harness.state.plans["h1"]
        harness.state.plans["h1"] = plan_state.model_copy(
            update={"patience": 0, "turn_limit": 4}
        )
        branch = harness.supervisor.workspace("h1")
        workdir = Path(branch.path)
        first_workspace = harness.supervisor.workspace_path("h1")
        first_context_ref = harness.state.plans["h1"].context_ref
        runner = PlanRunner(
            execution=ExecutionRuntime(
                project_root=tmp_path,
                environment_root=tmp_path,
                store=harness.store,
            ),
            store=harness.store,
            evaluator=TrustedEvaluator(
                DataScriptRunner(store=harness.store, workdir=tmp_path / "eval-run")
            ),
            workspace=harness.git,
            branch=branch,
            context=ExecutionContext(
                project_root=tmp_path,
                workspace_root=workdir,
                environment_root=tmp_path,
            ),
        )

        async def execute_plan(plan_id, state):
            return await runner.run_turn(
                plan_id,
                state,
                await harness.supervisor.plan_input(plan_id),
            )

        harness.supervisor._deps.research = replace(
            harness.supervisor._deps.research, plan=execute_plan
        )
        executions = []

        async def counted_execute_plan(plan_id, state):
            result = await execute_plan(plan_id, state)
            executions.append(result)
            return result

        harness.supervisor._deps.research = replace(
            harness.supervisor._deps.research, plan=counted_execute_plan
        )
        (workdir / "model.py").write_text(
            "from pathlib import Path\n"
            "from features import predictions\n"
            "Path('outputs/predictions').mkdir(parents=True, exist_ok=True)\n"
            "Path('outputs/predictions/predictions.csv').write_text("
            "predictions)\n",
            encoding="utf-8",
        )
        (workdir / "features.py").write_text(
            "predictions = '__athena_row_id,prediction\\nrow_1,1\\nrow_2,1\\n'\n",
            encoding="utf-8",
        )
        (workdir / "experiment.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "commands": [
                        (
                            [sys.executable, "-B", "-m", "model"]
                            if module_entry
                            else [sys.executable, "-B", "model.py"]
                        )
                    ],
                    "outputs": {"predictions": "outputs/predictions"},
                }
            ),
            encoding="utf-8",
        )

        harness.task = asyncio.create_task(harness.supervisor.run_search())
        await _eventually(lambda: len(harness.prompts["h1"]) == 1)
        harness.decisions["h1"].append("continue")
        harness.gates["h1"].set()

        # The first turn has a real trusted score (0.5); zero patience retains
        # the Plan and dispatches its next turn rather than ending the search.
        await _eventually(lambda: len(harness.prompts["h1"]) == 2)
        assert "h1" in harness.state.plans
        assert harness.state.plans["h1"].best_ref is not None
        assert harness.state.plans["h1"].context_ref == first_context_ref
        assert harness.supervisor.workspace_path("h1") == first_workspace
        assert harness.tree.get_experiment("exp_h1").status.value == "RUNNING"

        # The entrypoint stays unchanged: an imported module is still executable
        # source, and changing it must be eligible for a new trusted score.
        entrypoint_before = (workdir / "model.py").read_bytes()
        (workdir / "features.py").write_text(
            "predictions = '__athena_row_id,prediction\\nrow_1,1\\nrow_2,0\\n'\n"
            "# new candidate implementation\n",
            encoding="utf-8",
        )
        harness.decisions["h1"].append("continue")
        harness.gates["h1"].set()

        # The second turn improves the real frozen-evaluator score to 1.0.
        await _eventually(lambda: len(harness.prompts["h1"]) == 3)
        assert (workdir / "model.py").read_bytes() == entrypoint_before
        best_ref = harness.state.plans["h1"].best_ref
        assert best_ref is not None
        best = await load_best(best_ref, harness.store)
        assert best.metric == 1.0
        prompt = harness.prompts["h1"][2]
        assert "Historical best trusted SEARCH metric: 1.00000000" in prompt
        assert f"scored commit: {best.commit}" in prompt
        assert "local OOF/CV scores are diagnostics" in prompt

        # This unscored edit must neither replace the trusted best nor execute
        # merely because the Agent chose submit.
        (workdir / "model.py").write_text(
            "raise RuntimeError('submit must not execute this unscored edit')\n",
            encoding="utf-8",
        )
        harness.decisions["h1"].append("submit")
        harness.gates["h1"].set()

        await _eventually(lambda: "h1" not in harness.state.plans)
        if harness.task is not None:
            await asyncio.wait_for(harness.task, timeout=10)

        experiment = harness.tree.get_experiment("exp_h1")
        assert experiment.status.value == "SUCCEEDED"
        assert experiment.eval is not None
        assert experiment.eval.primary == 1.0
        assert experiment.commit == best.commit
        assert len(executions) == 2
        assert harness.supervisor.workspace_path("h1") == first_workspace
        assert len(harness.prompts["h1"]) == 3
    finally:
        await harness.supervisor.stop()
        await harness.close()

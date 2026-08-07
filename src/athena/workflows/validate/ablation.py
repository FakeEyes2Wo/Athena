"""Record ablations and one frozen final test as ResearchTree v2 experiments."""

import asyncio

from pydantic import BaseModel

from athena.core.workspace import GitWorkspace
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.contracts import new_id
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.evaluation.types import EvalSpec
from athena.workflows.search.code_agent import CodeAgent, CodeExecutionError


class ValidationResult(BaseModel):
    """Canonical IDs for one complete validation set."""

    sota_id: str
    ablation_ids: list[str]
    final_test_id: str


class Validator:
    """Execute isolated validation experiments without changing SOTA."""

    def __init__(
        self,
        workspace: GitWorkspace,
        code_agent: CodeAgent,
        eval_spec: EvalSpec,
    ) -> None:
        self._workspace = workspace
        self._code_agent = code_agent
        self._eval_spec = eval_spec

    @staticmethod
    def _validate_sota(sota_id: str, tree: ResearchTree) -> Experiment:
        sota = tree.get_experiment(sota_id)
        if tree.best_experiment_id() != sota_id:
            raise RuntimeError("VALIDATE requires the selected SOTA experiment")
        if sota.status is not ExperimentStatus.SUCCEEDED or sota.eval is None:
            raise RuntimeError("VALIDATE requires a successful SOTA with evaluation")
        if sota.plan.kind not in {"baseline", "search"}:
            raise RuntimeError("VALIDATE requires an eligible selected SOTA")
        return sota

    @staticmethod
    def _existing_success(
        tree: ResearchTree,
        experiment_ids: list[str],
        *,
        label: str,
    ) -> str | None:
        experiments = [tree.get_experiment(item_id) for item_id in experiment_ids]
        if any(
            item.status in {ExperimentStatus.PENDING, ExperimentStatus.RUNNING}
            for item in experiments
        ):
            raise RuntimeError(f"{label} validation is already active")
        succeeded = [
            item_id
            for item_id, item in zip(experiment_ids, experiments, strict=True)
            if item.status is ExperimentStatus.SUCCEEDED
        ]
        if len(succeeded) > 1:
            raise RuntimeError(f"duplicate successful {label} validation")
        return succeeded[0] if succeeded else None

    async def _execute(
        self,
        *,
        experiment_id: str,
        hypothesis: Hypothesis,
        plan: ExperimentPlan,
        sota_id: str,
        sota: Experiment,
        tree: ResearchTree,
        branch_kind: str,
    ) -> str:
        # tree 中存储的 hypothesis 必有 id（add_hypothesis 保证）
        assert hypothesis.id is not None
        worktree = await self._workspace.create(
            sota.commit,
            f"validate/{branch_kind}/{experiment_id}",
        )
        tree.add_experiment(
            experiment_id,
            Experiment(
                parent_id=sota_id,
                hypothesis_id=hypothesis.id,
                commit=sota.commit,
                plan=plan,
                gitwork=worktree,
            ),
        )
        tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)
        try:
            result = await self._code_agent.execute(
                experiment_id,
                hypothesis,
                plan,
                sota.commit,
                self._eval_spec,
                worktree,
            )
            tree.complete_experiment(
                experiment_id,
                eval=result.eval,
                verdict=None,
                artifacts={"diff": result.diff, "logs": result.logs},
                commit=result.commit,
            )
        except asyncio.CancelledError:
            tree.transition_experiment(experiment_id, ExperimentStatus.CANCELLED)
            raise
        except CodeExecutionError as exc:
            tree.attach_artifact(experiment_id, "logs", exc.logs)
            tree.transition_experiment(
                experiment_id,
                ExperimentStatus.FAILED,
                error=str(exc),
            )
            raise
        except Exception as exc:
            tree.transition_experiment(
                experiment_id,
                ExperimentStatus.FAILED,
                error=str(exc) or type(exc).__name__,
            )
            raise
        return experiment_id

    async def run(self, sota_id: str, tree: ResearchTree) -> ValidationResult:
        """Complete missing validation records and return their canonical IDs."""
        sota = self._validate_sota(sota_id, tree)
        hypotheses = tree.hypotheses_path(sota_id)
        validation_children = [
            child_id
            for child_id in tree.list_children(sota_id)
            if tree.get_experiment(child_id).plan.kind in {"ablation", "final-test"}
        ]

        ablation_ids: list[str] = []
        for hypothesis in hypotheses:
            matching = [
                child_id
                for child_id in validation_children
                if (
                    tree.get_experiment(child_id).plan.kind == "ablation"
                    and tree.get_experiment(child_id).hypothesis_id == hypothesis.id
                )
            ]
            existing = self._existing_success(
                tree,
                matching,
                label=f"ablation for {hypothesis.id}",
            )
            if existing is not None:
                ablation_ids.append(existing)
                continue
            experiment_id = new_id("exp_ablation")
            plan = ExperimentPlan(
                kind="ablation",
                change=f"Remove intervention for {hypothesis.id}",
                run_config_ref=f"artifact://runs/{experiment_id}/config",
                budget=sota.plan.budget,
                acceptance_rule=(
                    "Record the frozen validation metric without changing SOTA"
                ),
                rubrics=[
                    "Uses the frozen validation split",
                    "Does not access final-test data",
                ],
            )
            ablation_ids.append(
                await self._execute(
                    experiment_id=experiment_id,
                    hypothesis=hypothesis,
                    plan=plan,
                    sota_id=sota_id,
                    sota=sota,
                    tree=tree,
                    branch_kind="ablation",
                )
            )

        final_records = [
            child_id
            for child_id in validation_children
            if tree.get_experiment(child_id).plan.kind == "final-test"
        ]
        final_test_id = self._existing_success(
            tree,
            final_records,
            label="final-test",
        )
        if final_test_id is None:
            final_test_id = new_id("exp_final")
            plan = ExperimentPlan(
                kind="final-test",
                change="Evaluate the selected SOTA once on the frozen final-test split",
                run_config_ref=f"artifact://runs/{final_test_id}/config",
                budget={"runs": 1},
                acceptance_rule=(
                    "The frozen final-test evaluator completes exactly once"
                ),
                rubrics=[
                    "Reads the final-test split once",
                    "Does not tune from final-test output",
                ],
            )
            final_test_id = await self._execute(
                experiment_id=final_test_id,
                hypothesis=tree.get_hypothesis(sota.hypothesis_id),
                plan=plan,
                sota_id=sota_id,
                sota=sota,
                tree=tree,
                branch_kind="final-test",
            )

        return ValidationResult(
            sota_id=sota_id,
            ablation_ids=ablation_ids,
            final_test_id=final_test_id,
        )

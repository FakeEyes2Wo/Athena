"""Orchestrate SEARCH from candidate selection through an evidence-backed decision."""

import asyncio
from collections.abc import Awaitable, Callable

from athena.core.contracts import new_id
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkspace
from athena.data.types import DataProfile
from athena.evaluation import Comparator
from athena.evaluation.types import EvalSpec, EvaluationInputs
from athena.experiment.ranking import HypothesisRanker, ProximityGraph
from athena.experiment.supervisor import Supervisor
from athena.ideator import Ideator
from athena.research.budget import BudgetSnapshot, RunMode
from athena.workflows.search.code_agent import (
    CodeAgent,
    CodeExecutionError,
    CodegenResult,
)
from athena.workflows.search.idea_generation import PaperSearch, generate_hypotheses


async def _ready() -> None:
    return None


class SearchLoop:
    """Run each SEARCH candidate through the canonical v2 experiment lifecycle."""

    def __init__(
        self,
        tree: ResearchTree,
        workspace: GitWorkspace,
        eval_spec: EvalSpec,
        budget: BudgetSnapshot,
        mode: RunMode = RunMode(),
        *,
        data_profile: DataProfile,
        validation_inputs: EvaluationInputs,
        ideator: Ideator | None = None,
        ranker: HypothesisRanker | None = None,
        proximity: ProximityGraph | None = None,
        supervisor: Supervisor | None = None,
        comparator: Comparator | None = None,
        code_agent: CodeAgent | None = None,
        before_iteration: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._tree = tree
        self._workspace = workspace
        self._eval_spec = eval_spec
        self._budget = budget
        self._mode = mode
        self._data_profile = data_profile
        self._validation_inputs = validation_inputs
        self._ideator = ideator
        self._ranker = ranker or HypothesisRanker()
        self._proximity = proximity or ProximityGraph()
        self._supervisor = supervisor or Supervisor()
        self._comparator = comparator or Comparator()
        self._code_agent = code_agent or CodeAgent()
        self._before_iteration = before_iteration or _ready
        self._results: list[CodegenResult] = []

    def _successful_sota(self) -> tuple[str, Experiment]:
        experiment_id = self._tree.best_experiment_id()
        if experiment_id is None:
            raise RuntimeError("SEARCH requires a successful baseline")
        experiment = self._tree.get_experiment(experiment_id)
        if (
            experiment.status is not ExperimentStatus.SUCCEEDED
            or experiment.plan.kind not in {"baseline", "search"}
            or experiment.eval is None
        ):
            raise RuntimeError("SEARCH requires a successful baseline")
        return experiment_id, experiment

    async def _pending_hypotheses(self) -> list[Hypothesis]:
        pending = self._tree.pending_hypotheses()
        if pending:
            return pending
        if self._ideator is None:
            raise RuntimeError("SEARCH requires an Ideator")
        search = PaperSearch()
        papers = await search.search("machine learning " + self._eval_spec.primary.name)
        await generate_hypotheses(
            data_profile=self._data_profile,
            papers=papers,
            models=[],
            tree=self._tree,
            ideator=self._ideator,
        )
        return self._tree.pending_hypotheses()

    async def run(self) -> list[CodegenResult]:
        """Run SEARCH until its budget is exhausted or the supervisor stops it."""
        self._successful_sota()

        while not self._budget.is_exhausted:
            await self._before_iteration()
            pending = await self._pending_hypotheses()
            if not pending:
                break

            selected_id = self._ranker.select(pending, self._proximity)
            hypothesis = next(item for item in pending if item.id == selected_id)
            parent_id, current_sota = self._successful_sota()
            experiment_id = new_id("exp")
            worktree = await self._workspace.create(
                current_sota.commit,
                f"exp/{experiment_id}",
            )
            plan = ExperimentPlan(
                kind="search",
                change=hypothesis.intervention,
                rubrics=[hypothesis.expected_effect],
                run_config_ref=f"artifact://runs/{experiment_id}/config",
                budget={"max_trials": 1},
                acceptance_rule=(
                    f"Improve {self._eval_spec.primary.name} under the frozen protocol"
                ),
            )
            self._tree.add_experiment(
                experiment_id,
                Experiment(
                    parent_id=parent_id,
                    hypothesis_id=selected_id,
                    commit=current_sota.commit,
                    plan=plan,
                    gitwork=worktree,
                ),
            )
            self._tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)

            result: CodegenResult | None = None
            try:
                result = await self._code_agent.execute(
                    experiment_id,
                    hypothesis,
                    plan,
                    current_sota.commit,
                    self._eval_spec,
                    worktree,
                    inputs=self._validation_inputs,
                )
                assert current_sota.eval is not None, "SOTA 实验必有 eval"
                verdict = self._comparator.compare(
                    current_sota.eval,
                    result.eval,
                    direction=self._eval_spec.primary.direction,
                )
                self._tree.complete_experiment(
                    experiment_id,
                    eval=result.eval,
                    verdict=verdict,
                    artifacts={"diff": result.diff, "logs": result.logs},
                    commit=result.commit,
                )
            except asyncio.CancelledError:
                self._tree.transition_experiment(
                    experiment_id, ExperimentStatus.CANCELLED
                )
                raise
            except CodeExecutionError as exc:
                self._tree.attach_artifact(experiment_id, "logs", exc.logs)
                self._tree.transition_experiment(
                    experiment_id,
                    ExperimentStatus.FAILED,
                    error=str(exc),
                )
                raise
            except Exception as exc:
                if result is not None:
                    self._tree.attach_artifact(experiment_id, "diff", result.diff)
                    self._tree.attach_artifact(experiment_id, "logs", result.logs)
                self._tree.transition_experiment(
                    experiment_id,
                    ExperimentStatus.FAILED,
                    error=str(exc) or type(exc).__name__,
                )
                raise

            decision = self._supervisor.decide(verdict, self._budget)
            accepted = decision.action == "ACCEPT" and verdict.winner == "candidate"
            if accepted:
                self._tree.set_sota(experiment_id)
            self._tree.update_hypothesis_status(
                selected_id,
                "SUPPORTED" if accepted else "REFUTED",
            )

            parent_hypothesis_id = current_sota.hypothesis_id
            if verdict.winner == "candidate":
                comparison = (selected_id, parent_hypothesis_id, False)
            elif verdict.winner == "baseline":
                comparison = (parent_hypothesis_id, selected_id, False)
            else:
                comparison = (selected_id, parent_hypothesis_id, True)
            self._ranker.update([comparison])
            self._proximity.add(hypothesis)
            self._budget.consume(improved=accepted)
            self._results.append(result)

            if decision.action == "STOP":
                break
            if self._mode.hil:
                input("Press Enter to continue to next experiment...")

        return list(self._results)

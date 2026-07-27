"""Orchestrates the SEARCH phase: generate -> rank -> execute -> evaluate -> decide -> loop."""

from athena.core.schemas import Hypothesis, EvalSpec, ComparisonVerdict
from athena.core.budget import BudgetSnapshot, RunMode
from athena.core.ranking import HypothesisRanker, ProximityGraph
from athena.core.evaluation import Comparator
from athena.core.research.research_tree import ResearchTree, Experiment
from athena.core.gitutils.workspace import GitWorkspace
from athena.execution.supervisor import Supervisor, Decision
from athena.workflows.search.code_agent import CodeAgent, CodegenResult
from athena.workflows.search.idea_generation import generate_hypotheses, PaperSearch


class SearchLoop:
    """Orchestrates the SEARCH phase: generate -> rank -> execute -> evaluate -> decide -> loop."""

    def __init__(
        self,
        tree: ResearchTree,
        workspace: GitWorkspace,
        eval_spec: EvalSpec,
        budget: BudgetSnapshot,
        mode: RunMode = RunMode(),
    ):
        self._tree = tree
        self._workspace = workspace
        self._eval_spec = eval_spec
        self._budget = budget
        self._mode = mode
        self._ranker = HypothesisRanker()
        self._proximity = ProximityGraph()
        self._supervisor = Supervisor()
        self._comparator = Comparator()
        self._code_agent = CodeAgent()
        self._results: list[CodegenResult] = []

    async def run(self) -> list[CodegenResult]:
        while not self._budget.is_exhausted:
            # 1. Generate more hypotheses if needed
            if len(self._tree.pending_hypotheses()) < 2:
                search = PaperSearch()
                papers = await search.search(
                    "machine learning " + self._eval_spec.primary.name
                )
                await generate_hypotheses(
                    data_profile=None, papers=papers, models=[], tree=self._tree
                )

            # 2. Select best hypothesis
            pending = self._tree.pending_hypotheses()
            if not pending:
                break  # no more ideas to try
            selected_id = self._ranker.select(pending, self._proximity)
            hypothesis = next(h for h in pending if h.id == selected_id)

            # 3. Execute
            best = self._tree.best_experiment()
            parent_commit = best.exp.commit if best else "HEAD"
            wt = await self._workspace.create(parent_commit, f"exp/{hypothesis.id}")
            result = await self._code_agent.execute(
                hypothesis, parent_commit, self._eval_spec, wt
            )

            # 4. Compare with baseline
            if best and best.exp.result_as_float() is not None:
                # Create a synthetic baseline EvalResult
                from athena.core.schemas import EvalResult

                baseline_eval = EvalResult(
                    experiment_id=best.id,
                    primary=best.exp.result_as_float(),
                    per_sample=f"artifact://samples/{best.id}",
                )
                verdict = self._comparator.compare(baseline_eval, result.eval)
            else:
                verdict = ComparisonVerdict(
                    winner="candidate", p_value=0.0
                )  # first experiment

            # 5. Supervisor decides
            decision = self._supervisor.decide(verdict, self._budget)

            # 6. Update state
            self._budget.consume(improved=(verdict.winner == "candidate"))
            self._ranker.update(
                []
                if verdict.winner == "tie"
                else [
                    (
                        result.experiment_id,
                        (best.id if best else "root"),
                        verdict.winner == "candidate",
                    )
                ]
            )
            self._proximity.add(hypothesis)
            self._tree.add_hypothesis(
                Hypothesis(
                    id=hypothesis.id,
                    parent_id=hypothesis.parent_id,
                    statement=hypothesis.statement,
                    intervention=hypothesis.intervention,
                    expected_effect=hypothesis.expected_effect,
                    sources=hypothesis.sources,
                    status="SUPPORTED" if verdict.winner == "candidate" else "REFUTED",
                )
            )
            self._results.append(result)

            if decision.action == "STOP":
                break

            # HiL pause
            if self._mode.hil:
                input("Press Enter to continue to next experiment...")

        return self._results

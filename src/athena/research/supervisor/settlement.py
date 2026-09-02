"""Trusted metric settlement and hypothesis result projection for Plans."""

import json
import logging
from collections.abc import Awaitable, Callable

from athena.core.contracts import ArtifactRef
from athena.core.research_models import ComparisonVerdict, EvalResult
from athena.core.research_tree import ExperimentStatus
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.experiment import PlanTurnResult, load_best
from athena.research.supervisor.plans import PlanInput
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.scheduling import Outcome
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.statistics import (
    MetricEvidence,
    settle_statistically,
    two_sided_p_value,
)

logger = logging.getLogger(__name__)

LoadPlanInput = Callable[[str], Awaitable[PlanInput]]
SaveState = Callable[[], None]


def compare_metric(
    candidate: float,
    reference: float,
    direction: str,
    tolerance: float,
) -> Outcome:
    """Compare candidate and reference metrics using the configured direction."""
    delta = candidate - reference if direction == "maximize" else reference - candidate
    if delta > tolerance:
        return Outcome.WIN
    if delta < -tolerance:
        return Outcome.LOSS
    return Outcome.DRAW


def status_for_outcome(outcome: Outcome | None) -> str:
    """Map a trusted outcome to the conservative durable hypothesis status."""
    if outcome is Outcome.WIN:
        return "SUPPORTED"
    if outcome is Outcome.LOSS:
        return "REFUTED"
    return "INCONCLUSIVE"


class PlanSettlement:
    """Settle trusted Plan results and project experiment/tree state."""

    def __init__(
        self,
        owner: object,
        deps: SupervisorDeps,
        run: SupervisorRunState,
        *,
        plan_input: LoadPlanInput,
        save_state: SaveState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self._run = run
        self._plan_input = plan_input
        self._save_state = save_state

    @property
    def _state(self) -> ResearchState:
        return self._owner.state

    @property
    def _tree(self):
        return self._owner.tree

    async def settle_plan(
        self,
        plan_id: str,
        best_ref: ArtifactRef | None,
        result: PlanTurnResult | None,
    ) -> None:
        """Persist one final Experiment before removing its active Plan."""
        plan_input = await self._plan_input(plan_id)
        hypothesis = self._tree.get_hypothesis(plan_id)
        experiment_id = f"exp_{plan_id}"
        primary: float | None = None
        outcome: Outcome | None = None
        if best_ref is None:
            self._tree.transition_experiment(
                experiment_id,
                ExperimentStatus.FAILED,
                error="settled without a trusted result",
            )
        else:
            best = await load_best(best_ref, self._deps.runtime.store)
            comparison: ComparisonVerdict | None = None
            reference = plan_input.reference_metric
            if reference is None:
                outcome = None
            elif best.std_error is not None:
                verdict = settle_statistically(
                    MetricEvidence(
                        metric=best.metric,
                        std_error=best.std_error,
                        n=best.n,
                    ),
                    reference,
                    direction=plan_input.direction,
                    min_effect_size=plan_input.min_effect_size,
                    alpha=plan_input.alpha,
                    family_size=plan_input.family_size,
                )
                outcome = {
                    "SUPPORTED": Outcome.WIN,
                    "REFUTED": Outcome.LOSS,
                }.get(verdict)
                p_value = two_sided_p_value(best.metric, reference, best.std_error)
                if p_value is not None:
                    comparison = ComparisonVerdict(
                        winner={
                            "SUPPORTED": "candidate",
                            "REFUTED": "baseline",
                        }.get(verdict, "tie"),
                        p_value=p_value,
                    )
            else:
                outcome = compare_metric(
                    best.metric,
                    reference,
                    plan_input.direction,
                    plan_input.tolerance,
                )
            evidence_ref = best.evidence_ref
            artifacts = {"evidence": evidence_ref}
            try:
                evidence = json.loads(
                    await self._deps.runtime.store.get_text(evidence_ref)
                )
            except (json.JSONDecodeError, OSError, ValueError):
                evidence = {}
            for key in (
                "predictions_ref",
                "metrics_ref",
                "report_ref",
                "exploration_ref",
            ):
                ref = evidence.get(key) if isinstance(evidence, dict) else None
                if isinstance(ref, str):
                    artifacts[key.removesuffix("_ref")] = ref
            if (
                "predictions" not in artifacts
                and result is not None
                and result.predictions_ref is not None
            ):
                artifacts["predictions"] = result.predictions_ref
            if (
                "report" not in artifacts
                and result is not None
                and result.report_ref is not None
            ):
                artifacts["report"] = result.report_ref
            primary = best.metric
            self._tree.complete_experiment(
                experiment_id,
                eval=EvalResult(
                    experiment_id=experiment_id,
                    primary=best.metric,
                    per_sample=evidence_ref,
                ),
                verdict=comparison,
                artifacts=artifacts,
                commit=best.commit,
            )
        if outcome is None:
            self._tree.update_hypothesis_status(plan_id, "INCONCLUSIVE")
        else:
            hypothesis.priority = self._deps.search.scheduler.settle(
                plan_input.reference_priority, outcome
            )
            self._tree.update_hypothesis_status(plan_id, status_for_outcome(outcome))
        if primary is not None:
            sota_id = self._tree.best_experiment_id()
            if sota_id is None:
                self._tree.set_sota(experiment_id)
            else:
                sota = self._tree.get_experiment(sota_id)
                if (
                    sota.eval is None
                    or compare_metric(
                        primary,
                        sota.eval.primary,
                        plan_input.direction,
                        plan_input.tolerance,
                    )
                    is Outcome.WIN
                ):
                    if comparison is not None and comparison.winner != "candidate":
                        logger.warning(
                            "SOTA moved to %s on a point estimate (%.4f vs %.4f) "
                            "while the interval verdict was %s (p=%.3f, "
                            "family_size=%d). The pointer is operational, not a "
                            "demonstrated improvement.",
                            experiment_id,
                            primary,
                            sota.eval.primary if sota.eval else float("nan"),
                            comparison.winner,
                            comparison.p_value,
                            plan_input.family_size,
                        )
                    self._tree.set_sota(experiment_id)
        self._tree.save(self._deps.paths.tree_path)
        self._state.plans.pop(plan_id)
        self._save_state()
        if self._deps.phases.on_plan_settled is not None:
            await self._deps.phases.on_plan_settled(plan_id)
        try:
            await self._deps.runtime.agents.reap(plan_id)
        except Exception:
            logger.warning(
                "failed to reap settled Plan agent %s", plan_id, exc_info=True
            )


__all__ = ["PlanSettlement", "compare_metric", "status_for_outcome"]

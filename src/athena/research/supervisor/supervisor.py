"""Explicit facade for Athena's autonomous research Supervisor."""

import logging
from pathlib import Path
from typing import Literal

from athena.core.contracts import ArtifactRef
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.experiment_documents import ProjectionContext
from athena.research.supervisor.deps import (
    SupervisorDeps,
    ValidationMode,
)
from athena.research.supervisor.phases import PhaseMachine
from athena.research.supervisor.plan_lifecycle import PlanLifecycle
from athena.research.supervisor.plans import PlanInput
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.search_loop import SearchLoop
from athena.research.supervisor.state import ResearchState

logger = logging.getLogger(__name__)

_DOCUMENTS_STALE_MESSAGE = (
    "Experiment documents could not be refreshed; canonical research state is "
    "safe and the documents will be rebuilt on recovery."
)


class Supervisor:
    """Own the only durable ResearchState and ResearchTree mutation loop."""

    def __init__(
        self,
        *,
        state: ResearchState,
        tree: ResearchTree,
        deps: SupervisorDeps,
    ) -> None:
        self.state = state
        self.tree = tree
        self._deps = deps
        self._run = SupervisorRunState(lambda: self.state)
        self._plans = PlanLifecycle(self, deps, self._run)
        self._search = SearchLoop(
            self, deps, self._run, self._plans, run_turn=self._plans.run_turn
        )
        self._phases = PhaseMachine(self, deps, self._run, self._plans, self._search)

    @property
    def evaluator_ref(self) -> ArtifactRef | None:
        """Return the frozen evaluator used by PREPARE and SEARCH."""
        return self.state.evaluator_ref

    @evaluator_ref.setter
    def evaluator_ref(self, value: ArtifactRef | None) -> None:
        """Replace the frozen evaluator used by PREPARE and SEARCH."""
        self.state.evaluator_ref = value

    @property
    def final_evaluator_ref(self) -> ArtifactRef | None:
        """Return the evaluator reserved for final validation."""
        return self.state.final_evaluator_ref

    @final_evaluator_ref.setter
    def final_evaluator_ref(self, value: ArtifactRef | None) -> None:
        """Replace the evaluator reserved for final validation."""
        self.state.final_evaluator_ref = value

    @property
    def running_plan_ids(self) -> tuple[str, ...]:
        """Return Plan IDs with a live local turn task."""
        return self._run.running_plan_ids

    @property
    def next_hypothesis_id(self) -> str | None:
        """Return the pending one-shot manual Hypothesis selection."""
        return self._run.next_hypothesis_id

    @property
    def kaggle_enabled(self) -> bool:
        """Report whether this run enables Kaggle integration."""
        return self._run.kaggle_enabled

    @property
    def kaggle_download(self) -> bool:
        """Report whether enabled Kaggle integration may download data."""
        return self._run.kaggle_download

    def is_stopped(self) -> bool:
        """Report whether the Human explicitly stopped the durable run."""
        return self._run.is_stopped()

    async def record_guidance(self, text: str, scope: str) -> dict[str, object]:
        """Record Human guidance for one or all later Plan inputs."""
        return await self._run.record_guidance(text, scope)

    async def start_plan(self, hypothesis_id: str) -> str:
        """Create the stable Plan identity for one Hypothesis."""
        return await self._plans.start_plan(hypothesis_id)

    async def plan_input(self, plan_id: str) -> PlanInput:
        """Load the immutable input frozen for an active Plan."""
        return await self._plans.plan_input(plan_id)

    def workspace_path(self, plan_id: str) -> Path:
        """Return the worktree path bound to an active Plan."""
        return self._plans.workspace_path(plan_id)

    def workspace(self, plan_id: str) -> GitWorkBranch:
        """Return the Git worktree identity bound to an active Plan."""
        return self._plans.workspace(plan_id)

    def plan_identity(self, plan_id: str) -> dict[str, str]:
        """Return stable Agent, workspace, and log identities for a Plan."""
        return self._plans.plan_identity(plan_id)

    async def recover(self, state: ResearchState | None = None) -> ResearchState:
        """Reconcile durable Plans and live Agent threads after restart."""
        recovered = await self._plans.recover(state)
        await self._rebuild_documents()
        return recovered

    async def _publish_document_outcome(self, success: bool) -> None:
        """Publish one sanitized warning when derived documents are stale."""
        if success:
            return
        try:
            await self._deps.phases.publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "error",
                    "text": _DOCUMENTS_STALE_MESSAGE,
                },
            )
        except Exception:
            logger.warning(
                "failed to publish document projection warning", exc_info=True
            )

    async def _rebuild_documents(self) -> None:
        """Rebuild derived documents from the current canonical state."""
        try:
            outcome = self._deps.runtime.documents.rebuild(
                ProjectionContext(self.tree, self.state, self._deps.search.direction)
            )
        except Exception:
            logger.warning("document rebuild failed", exc_info=True)
            outcome = False
        await self._publish_document_outcome(outcome)

    async def propose_hypothesis(self, **payload: object) -> dict[str, object]:
        """Validate and register one Supervisor-proposed Hypothesis."""
        return await self._plans.propose_hypothesis(**payload)

    async def register_hypotheses(
        self, hypotheses: list[Hypothesis]
    ) -> dict[str, object]:
        """Register a batch of Ideator Hypotheses under the current SOTA."""
        return await self._plans.register_hypotheses(hypotheses)

    async def checkpoint_evaluator(self, ref: ArtifactRef) -> dict[str, object]:
        """Persist the frozen evaluator produced by PREPARE."""
        return await self._plans.checkpoint_evaluator(ref)

    async def checkpoint_final_evaluator(self, ref: ArtifactRef) -> dict[str, object]:
        """Persist the evaluator reserved for final validation."""
        return await self._plans.checkpoint_final_evaluator(ref)

    async def dispatch_general(self, task: str) -> dict[str, object]:
        """Dispatch or reuse a General Agent task checkpoint."""
        return await self._plans.dispatch_general(task)

    async def run_search(self) -> None:
        """Run the rolling SEARCH scheduler until it parks or completes."""
        await self._search.run_search()

    async def select_next_hypothesis(self, hypothesis_id: str) -> dict[str, object]:
        """Queue one validated Hypothesis for manual SEARCH."""
        return await self._search.select_next_hypothesis(hypothesis_id)

    async def configure_search(self, **payload: object) -> dict[str, object]:
        """Apply SEARCH budget and concurrency settings."""
        return await self._search.configure_search(**payload)

    def configure_options(
        self,
        *,
        direction: Literal["maximize", "minimize"] | None = None,
        tolerance: float | None = None,
        validation_mode: ValidationMode | None = None,
    ) -> None:
        """Update runtime comparison and validation policy."""
        if direction is not None:
            self._deps.search.direction = direction
        if tolerance is not None:
            self._deps.search.tolerance = tolerance
        if validation_mode is not None:
            self._deps.phases.validation_mode = validation_mode

    async def update_waiting_plan_budget(self, **payload: object) -> dict[str, object]:
        """Extend one exhausted Plan so SEARCH may resume it."""
        return await self._search.update_waiting_plan_budget(**payload)

    async def set_manual_mode(self, manual: bool) -> dict[str, object]:
        """Toggle automatic or Human-selected SEARCH scheduling."""
        return await self._search.set_manual_mode(manual)

    async def start(self) -> None:
        """Run the current research phase lifecycle."""
        if self.state.phase == "PREPARE":
            await self._rebuild_documents()
        await self._phases.start()

    async def continue_phase(self) -> None:
        """Continue the durable phase after an interactive decision."""
        await self._phases.continue_phase()

    async def checkpoint_validation(self, result_ref: ArtifactRef) -> None:
        """Persist one recoverable validation result."""
        await self._phases.checkpoint_validation(result_ref)

    async def set_phase_decision(self, decision: str) -> dict[str, object]:
        """Apply a validated SEARCH or VALIDATE phase decision."""
        return await self._phases.set_phase_decision(decision)

    async def pause(self) -> str:
        """Pause new Agent dispatch while retaining unfinished work."""
        return await self._phases.pause()

    async def resume(self, *, restarting: bool = False) -> str:
        """Resume Agent dispatch after an explicit Human command."""
        return await self._phases.resume(restarting=restarting)

    async def suspend(self) -> str:
        """Park a live run before its runtime is torn down."""
        return await self._phases.suspend()

    async def request_stop(self) -> str:
        """Persist an explicit Human stop and interrupt active Plans."""
        return await self._phases.request_stop()

    async def stop(self) -> None:
        """Park scheduling and interrupt active local Plan turns."""
        await self._phases.stop()

    async def message(self, text: str) -> str:
        """Delegate ordinary Human text to the Supervisor Agent."""
        return await self._deps.research.supervisor(text)

    async def set_kaggle_enabled(
        self, enabled: bool, download: bool = True
    ) -> dict[str, object]:
        """Persist the Kaggle integration and download policy."""
        return await self._phases.set_kaggle_enabled(enabled, download)

    async def record_task_understanding(self, **payload: object) -> dict[str, object]:
        """Persist the structured task understanding projection."""
        return await self._phases.record_task_understanding(**payload)

    async def read_hypotheses(self) -> dict[str, object]:
        """Return pending Hypotheses, SOTA, and SEARCH attempt counts."""
        return await self._phases.read_hypotheses()

    async def read_state(self) -> dict[str, object]:
        """Return the current research phase and configuration."""
        return await self._phases.read_state()

    async def read_plans(self) -> dict[str, object]:
        """Return active Plan budgets and locally running IDs."""
        return await self._phases.read_plans()

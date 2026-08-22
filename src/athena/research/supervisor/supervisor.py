"""Thin facade for Athena's autonomous research Supervisor.

The Supervisor class intentionally keeps only a small set of collaborators.
Most public API methods are delegated dynamically to those collaborators, so
the class stays small while preserving the same callable surface.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.research_tree import ResearchTree
from athena.core.workspace import GitWorkspace
from athena.research.supervisor.deps import (
    GeneralTurn,
    IdeatorTurn,
    PlanTurn,
    PreparePhase,
    Publish,
    PublishAgentEvent,
    SupervisorDeps,
    SupervisorTurn,
    ValidationPhase,
)
from athena.research.supervisor.experiment import (
    PlanTurnResult,
    handoff_block,
    load_agent_result,
)
from athena.research.supervisor.phases import PhaseMachine, _final_report_text
from athena.research.supervisor.plan_lifecycle import PlanLifecycle
from athena.research.supervisor.plans import PlanDecision, wait_run_events
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.search_loop import SearchLoop
from athena.research.supervisor.state import ResearchState

# Private-name compatibility mappings used by ``__setattr__`` so existing tests
# and callers that mutate supervisor internals keep updating the collaborators.
_DEPS_ATTRS = {
    "_project_root": "project_root",
    "_state_path": "state_path",
    "_tree_path": "tree_path",
    "_store": "store",
    "_agents": "agents",
    "_workspaces": "workspaces",
    "_scheduler": "scheduler",
    "_recovery": "recovery",
    "_evaluator_ref": "evaluator_ref",
    "_direction": "direction",
    "_tolerance": "tolerance",
    "_run_plan_turn": "run_plan_turn",
    "_run_supervisor_turn": "run_supervisor_turn",
    "_run_ideator_turn": "run_ideator_turn",
    "_run_general_turn": "run_general_turn",
    "_publish": "publish",
    "_auto_validate": "auto_validate",
    "_run_prepare_phase": "run_prepare_phase",
    "_run_validation_phase": "run_validation_phase",
    "_publish_agent_event": "publish_agent_event",
}
_RUN_ATTRS = {
    "_branches": "_branches",
    "_next_guidance": "_next_guidance",
    "_persistent_guidance": "_persistent_guidance",
    "_running": "_running",
    "_next_hypothesis_id": "_next_hypothesis_id",
    "_stopped": "_stopped",
    "_kaggle_download": "_kaggle_download",
    "_wake": "_wake",
    "_search_task": "_search_task",
}


@dataclass(frozen=True)
class _CompletedTurn:
    plan_id: str
    decision: PlanDecision | None
    result: PlanTurnResult | None


class Supervisor:
    """Own the only durable ResearchState and ResearchTree mutation loop."""

    def __init__(
        self,
        *,
        project_root: Path,
        state_root: Path | None = None,
        state: ResearchState,
        tree: ResearchTree,
        store: ArtifactStore,
        agents: object,
        workspaces: GitWorkspace,
        scheduler: Scheduler,
        recovery: Recovery,
        evaluator_ref: ArtifactRef | None,
        run_plan_turn: PlanTurn,
        run_supervisor_turn: SupervisorTurn,
        publish: Publish,
        auto_validate: bool = False,
        run_ideator_turn: IdeatorTurn | None = None,
        run_general_turn: GeneralTurn | None = None,
        direction: Literal["maximize", "minimize"] = "maximize",
        tolerance: float = 0.0,
        run_prepare_phase: PreparePhase | None = None,
        run_validation_phase: ValidationPhase | None = None,
        publish_agent_event: PublishAgentEvent | None = None,
    ) -> None:
        self.state = state
        self.tree = tree
        project_root = Path(project_root)
        _athena = (
            Path(state_root)
            if state_root is not None
            else project_root / ".athena"
        )
        deps = SupervisorDeps(
            project_root=project_root,
            state_path=_athena / "state.json",
            tree_path=_athena / "research_tree.json",
            store=store,
            agents=agents,
            workspaces=workspaces,
            scheduler=scheduler,
            recovery=recovery,
            evaluator_ref=(
                evaluator_ref if evaluator_ref is not None else state.evaluator_ref
            ),
            direction=direction,
            tolerance=tolerance,
            run_plan_turn=run_plan_turn,
            run_supervisor_turn=run_supervisor_turn,
            publish=publish,
            auto_validate=auto_validate,
            run_ideator_turn=run_ideator_turn,
            run_general_turn=run_general_turn,
            run_prepare_phase=run_prepare_phase,
            run_validation_phase=run_validation_phase,
            publish_agent_event=publish_agent_event,
        )
        run = SupervisorRunState(state.kaggle_download)
        plans = PlanLifecycle(self, deps, run)
        search = SearchLoop(self, deps, run, plans)
        phases = PhaseMachine(self, deps, run, plans, search)
        self._deps = deps
        self._run = run
        self._plans = plans
        self._search = search
        self._phases = phases

    def __setattr__(self, name: str, value: object) -> None:
        # Collaborator fields are real attributes; everything else is either a
        # compatibility private alias or a plain test override.
        if name in {
            "state",
            "tree",
            "_deps",
            "_run",
            "_plans",
            "_search",
            "_phases",
        }:
            object.__setattr__(self, name, value)
            return
        try:
            deps = object.__getattribute__(self, "_deps")
        except AttributeError:
            object.__setattr__(self, name, value)
            return
        if name in _DEPS_ATTRS:
            setattr(deps, _DEPS_ATTRS[name], value)
            return
        try:
            run = object.__getattribute__(self, "_run")
        except AttributeError:
            object.__setattr__(self, name, value)
            return
        if name in _RUN_ATTRS:
            setattr(run, _RUN_ATTRS[name], value)
            return
        object.__setattr__(self, name, value)

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            run = object.__getattribute__(self, "_run")
            plans = object.__getattribute__(self, "_plans")
            search = object.__getattribute__(self, "_search")
            phases = object.__getattribute__(self, "_phases")
            deps = object.__getattribute__(self, "_deps")
        except AttributeError:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            ) from None
        for holder in (run, plans, search, phases):
            try:
                return getattr(holder, name)
            except AttributeError:
                pass
        if name in _DEPS_ATTRS:
            return getattr(deps, _DEPS_ATTRS[name])
        if name in _RUN_ATTRS:
            return getattr(run, _RUN_ATTRS[name])
        try:
            return getattr(deps, name)
        except AttributeError:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            ) from None

    def _hypothesis_block(self, plan_id: str) -> str:
        """本 Plan 要检验的那条假设，拼进 prompt 正文。

        ``plan_id`` 就是 ``hypothesis_id``（见 ``start_plan``）。树里取不到时返回空串
        而不是抛异常：少一段上下文该降级，不该让整条 Plan 挂掉。
        """
        try:
            hypothesis = self.tree.get_hypothesis(plan_id)
        except KeyError:
            return ""
        from athena.research.supervisor.experiment import hypothesis_block

        return hypothesis_block(
            hypothesis.statement, hypothesis.intervention, hypothesis.expected_effect
        )

    async def _run_one_turn(self, plan_id: str) -> _CompletedTurn:
        """Spend one turn durably, run the Agent, then execute trusted scoring."""
        state = self.state.plans[plan_id]
        state = state.model_copy(update={"turns_used": state.turns_used + 1})
        self.state.plans[plan_id] = state
        await self._persist_state()
        try:
            run_id = await self._agents.followup(
                plan_id,
                {
                    "content": (
                        f"Continue Plan {plan_id}. Turns used: {state.turns_used}; "
                        f"turn limit: {state.turn_limit}; patience: {state.patience}; "
                        f"stale rounds: {state.stale_rounds}."
                        # 假设与契约都必须走 content：context_refs 到不了 model
                        # （见 experiment.hypothesis_block / handoff_block）。
                        + self._hypothesis_block(plan_id)
                        + handoff_block(await self._plan_handoff(plan_id))
                        + self._corpus_block(plan_id)
                    ),
                    "context_refs": [state.context_ref],
                },
            )
            if self._publish_agent_event is None:
                summary = await self._agents.wait_run(run_id)
            else:
                summary = await wait_run_events(
                    self._agents,
                    run_id,
                    lambda kind, ref, data: self._publish_agent_event(
                        plan_id, kind, ref, data
                    ),
                )
            decision = await load_agent_result(summary, self._store, PlanDecision)
            if decision is None:
                return _CompletedTurn(plan_id, None, None)
        except Exception:
            return _CompletedTurn(plan_id, None, None)
        if decision.decision == "abandon" and state.best_ref is None:
            return _CompletedTurn(plan_id, decision, None)
        result = await self._run_plan_turn(plan_id, state)
        return _CompletedTurn(plan_id, decision, result)


__all__ = [
    "GeneralTurn",
    "IdeatorTurn",
    "PlanTurn",
    "Publish",
    "Supervisor",
    "SupervisorTurn",
    "_final_report_text",
]

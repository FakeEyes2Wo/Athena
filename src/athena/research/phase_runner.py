"""Phase execution for the research lifecycle (PREPARE / VALIDATE / plan turn)."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from athena.agents.ideator_agent import HandoffResult
from athena.agents.task_agents import register_validate_agent
from athena.execution.runtime import ExecutionContext
from athena.research.contracts import ValidationResult
from athena.research.prepare_phase import run_prepare_phase
from athena.research.supervisor.experiment import (
    PlanRunner,
    PlanTurnResult,
    load_agent_result,
)
from athena.research.supervisor.plans import wait_run_events
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.validation import (
    ValidationDiffReview,
    ValidationInput,
    run_validation_plan,
    validation_key,
)
from athena.utils.single_turn_chat import single_turn_chat

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime


class PhaseRunner:
    """Run PREPARE / VALIDATE / plan phases via the runtime's shared infra."""

    def __init__(self, runtime: "ResearchRuntime") -> None:
        self._runtime = runtime

    async def run_plan_turn(self, plan_id: str, state: Any) -> PlanTurnResult:
        """Execute one Plan turn through the configured plan runner."""
        rt = self._runtime
        if rt.plan_turn is not None:
            return await rt.plan_turn(plan_id, state)
        plan_input = await rt.supervisor.plan_input(plan_id)
        workspace_path = rt.supervisor.workspace_path(plan_id)
        execution = await rt.execution_for(plan_id, workspace_path)
        runner = PlanRunner(
            execution=execution,
            store=rt.store,
            evaluator=rt.evaluator,
            workspace=rt.git,
            branch=rt.supervisor.workspace(plan_id),
            context=ExecutionContext(
                project_root=rt.root,
                workspace_root=workspace_path,
                environment_root=rt.root,
                experiment_id=plan_id,
            ),
            direction=plan_input.direction,
            timeout_s=rt.state.experiment_timeout_s,
            placement=lambda: rt.placement_for(plan_id),
        )
        return await runner.run_turn(plan_id, state, plan_input)

    async def _run_handoff_agent(
        self,
        *,
        agent_id: str,
        agent_type: str,
        workspace: str,
        output_file: str,
        content: str,
        reap_after: bool = False,
    ) -> str:
        """Run one handoff-producing agent and return the output file text."""
        rt = self._runtime
        agents = getattr(rt, "agents", None) or getattr(rt, "_agents")
        store = getattr(rt, "store", None) or getattr(rt, "_store")
        request = {"content": content, "context_refs": []}
        try:
            if agents.has_agent(agent_id):
                run_id = await agents.followup(agent_id, request)
            else:
                _id, run_id = await agents.create_root(
                    agent_type, request, agent_id=agent_id, name=agent_id
                )

            async def publish(kind: str, ref: str, data: dict | None = None) -> None:
                events_bus = getattr(rt, "events", None) or getattr(
                    rt, "_events_bus", None
                )
                if events_bus is not None:
                    await events_bus.project_agent_event(agent_id, kind, ref, data)

            summary = await wait_run_events(agents, run_id, publish)
            result = await load_agent_result(summary, store, HandoffResult)
            path = Path(workspace) / output_file
            if result is None:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
                raise RuntimeError(
                    summary.error or f"{agent_type} handoff agent failed"
                )
            if not path.is_file():
                raise RuntimeError(f"{agent_type} did not write {output_file}")
            return path.read_text(encoding="utf-8")
        finally:
            if reap_after:
                try:
                    await agents.reap(agent_id)
                except Exception:  # noqa: BLE001,S110
                    pass

    async def run_prepare_phase(self) -> PrepareResult:
        """Run PREPARE and return the trusted baseline result."""
        return await run_prepare_phase(self._runtime, self._run_handoff_agent)

    async def run_validation_phase(
        self, sota_commit: str, metric: float
    ) -> ValidationResult:
        """Run VALIDATE against the disjoint frozen final evaluator."""
        rt = self._runtime
        if rt.validation_phase is not None:
            return await rt.validation_phase(sota_commit, metric)
        if rt.provider is None:
            raise RuntimeError("VALIDATE requires a registered Agent provider")
        evaluator_ref = rt.supervisor.evaluator_ref
        final_evaluator_ref = rt.supervisor.final_evaluator_ref
        if evaluator_ref is None or final_evaluator_ref is None:
            raise RuntimeError(
                "VALIDATE requires disjoint frozen search and final evaluators"
            )
        if evaluator_ref == final_evaluator_ref:
            raise RuntimeError("VALIDATE final evaluator must differ from SEARCH")
        workspace = await rt.git.create(sota_commit, "athena/validate")
        if not rt.registry.contains("validate"):
            register_validate_agent(
                rt.registry,
                provider=rt.provider,
                artifacts=rt.store,
                workspace=Path(workspace.path),
                runtime=rt.execution,
            )
        sota_id = rt.tree.best_experiment_id()
        if sota_id is None:
            raise RuntimeError("VALIDATE requires a trusted SOTA baseline")
        experiment = rt.tree.get_experiment(sota_id)
        hypothesis = rt.tree.get_hypothesis(experiment.hypothesis_id)
        sota_context = {
            "experiment_id": sota_id,
            "statement": hypothesis.statement,
            "intervention": hypothesis.intervention,
            "expected_effect": hypothesis.expected_effect,
            "commit": experiment.commit,
            "metric": experiment.eval.primary if experiment.eval else None,
        }
        frozen = ValidationInput(
            sota_commit=sota_commit,
            reference_metric=metric,
            direction=rt.direction,
            final_evaluator_ref=final_evaluator_ref,
            validation_key=validation_key(
                sota_commit, metric, rt.direction, final_evaluator_ref
            ),
            sota_context=sota_context,
        )
        result_ref = None
        if rt.state.validation is not None:
            candidate = rt.state.validation.get("result_ref")
            if isinstance(candidate, str):
                result_ref = candidate
        return await run_validation_plan(
            input=frozen,
            agents=rt.agents,
            git=rt.git,
            workspace=workspace,
            execution=rt.execution,
            evaluator=rt.evaluator,
            store=rt.store,
            independent_review=self.review_validation_diff,
            result_ref=result_ref,
            checkpoint=rt.supervisor.checkpoint_validation,
            publish=lambda kind, ref, data: rt.events.project_agent_event(
                "validate", kind, ref, data
            ),
        )

    async def review_validation_diff(self, prompt: str) -> ValidationDiffReview:
        """Independently review and accept/reject a proposed validation diff."""
        rt = self._runtime
        if rt.model is None:
            raise RuntimeError("independent validation review requires a model")
        answer = await single_turn_chat(
            prompt,
            model=rt.model,
            client=rt.client,
            system_prompt=(
                "Independently review the proposed VALIDATE diff. Accept only "
                "runtime compatibility repairs and reject changes to model, data, "
                "features, preprocessing, training, or final-label access. Return "
                'JSON only: {"accepted":true|false,"reason":"..."}.'
            ),
            max_turns=200,
        )
        return ValidationDiffReview.model_validate_json(answer)

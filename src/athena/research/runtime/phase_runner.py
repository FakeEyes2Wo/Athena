"""Phase execution for the research lifecycle (PREPARE / VALIDATE / plan turn).

拆自 ``ResearchRuntime``：把 phase 编排（run_plan_turn / run_prepare_phase /
run_validation_phase / review_validation_diff）集中到一个组合单元。持有
``runtime`` 引用以访问组合根的共享基础设施；避免在构造期重复注入十余个
可变字段。
"""

import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

from athena.agents.ideator_agent import HandoffResult
from athena.agents.task_agents import register_validate_agent
from athena.execution.runtime import ExecutionContext
from athena.research.clarification.context import confirmed_task_context_block
from athena.research.contracts import ValidationResult
from athena.research.prepare.authority import BaselineAuthorityError
from athena.research.prepare.baseline import _load_authoritative_baseline_record
from athena.research.prepare.orchestrator import run_prepare_phase
from athena.research.supervisor.events import wait_run_events
from athena.research.supervisor.experiment import (
    PlanRunner,
    PlanTurnResult,
    load_agent_result,
)
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.validation import (
    ValidationDeps,
    run_validation_plan,
    validation_key,
)
from athena.research.supervisor.validation_contracts import (
    ValidationDiffReview,
    ValidationInput,
    ValidationOptions,
)
from athena.utils.single_turn_chat import single_turn_chat

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime


async def _load_trusted_prepare_score(
    store: Any, evidence_ref: str
) -> tuple[float, str]:
    """Load the trusted PlanRunner score from its content-addressed evidence."""
    try:
        evidence = json.loads(await store.get_text(evidence_ref))
        if not isinstance(evidence, dict) or evidence.get("plan") != "prepare":
            raise ValueError("evidence is not a PREPARE score")
        metric = evidence["metric"]
        commit = evidence["commit"]
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(metric)
        ):
            raise ValueError("evidence metric is not finite")
        if not isinstance(commit, str) or not commit.strip():
            raise ValueError("evidence commit is blank")
    except Exception as exc:  # noqa: BLE001 - artifact store trust boundary
        raise BaselineAuthorityError(
            "trusted PREPARE evidence is missing, corrupt, or invalid"
        ) from exc
    return float(metric), commit


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
        search_features = rt.workspaces_root / "data_split" / "search_features.csv"
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
                predict_features=search_features if search_features.is_file() else None,
            ),
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
        """Run one handoff-producing agent and return the output file text.

        ``reap_after`` should be True only on the agent's last use; it releases
        the one-shot thread and facade metadata immediately.
        """
        rt = self._runtime
        request = {"content": content, "context_refs": []}
        try:
            if rt.agents.has_agent(agent_id):
                run_id = await rt.agents.followup(agent_id, request)
            else:
                _id, run_id = await rt.agents.create_root(
                    agent_type, request, agent_id=agent_id, name=agent_id
                )

            async def publish(kind: str, ref: str, data: dict | None = None) -> None:
                """Forward one agent journal event to the runtime event bus.

                Must be a coroutine function. ``forward_run_events`` does
                ``await publish(...)`` on every journal event, and
                ``project_agent_event`` is itself async. As a plain ``def`` this
                returned None, so the first event of every handoff run raised
                ``object NoneType can't be used in 'await' expression`` -- and
                the coroutine it dropped on the floor meant the events were
                never projected either.

                Real run (2026-08-29): all nine EDA_REPORT_*.md were written,
                then the handoff died on its first event and PREPARE fell back
                to "EDA failed, degrade to the raw task text". The reports were
                right there on disk and nothing read them.
                """
                events_bus = getattr(rt, "events", None)
                if events_bus is not None:
                    await events_bus.project_agent_event(agent_id, kind, ref, data)

            summary = await wait_run_events(rt.agents, run_id, publish)
            result = await load_agent_result(summary, rt.store, HandoffResult)
            path = Path(workspace) / output_file
            # 文件已落盘但结果解析失败时，仍视为成功，避免“文件存在却标红”。
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
                    await rt.agents.reap(agent_id)
                except Exception:
                    logger.warning(
                        "failed to reap handoff Agent %s", agent_id, exc_info=True
                    )

    async def run_prepare_phase(self) -> PrepareResult:
        """Run the PREPARE phase and return the trusted baseline result."""
        return await run_prepare_phase(self._runtime, self._run_handoff_agent)

    async def baseline_resume_is_attested(self) -> bool:
        """Authorize PREPARE resume only from exact external completion evidence."""
        rt = self._runtime
        authority = rt.baseline_authority
        if authority is None:
            raise BaselineAuthorityError(
                "PREPARE resume requires an external baseline authority capability"
            )
        eda_dir = rt.state.eda_dir
        if not isinstance(eda_dir, str) or not eda_dir.strip():
            raise BaselineAuthorityError(
                "PREPARE resume has no durable baseline workspace"
            )
        workspace = Path(eda_dir)
        if not workspace.is_absolute():
            workspace = rt.root / workspace
        workspace = workspace.resolve()
        if not workspace.is_relative_to(rt.root.resolve()):
            raise BaselineAuthorityError(
                "PREPARE resume baseline workspace escapes the project root"
            )

        loaded = await _load_authoritative_baseline_record(workspace, authority)
        if loaded is None:
            return False
        _verified, sealed = loaded
        attestation = sealed.attestation
        if sealed.generation != 1 or attestation is None:
            return False

        sota_id = rt.tree.best_experiment_id()
        if sota_id is None:
            return False
        experiment = rt.tree.get_experiment(sota_id)
        if experiment.eval is None or experiment.plan.kind != "baseline":
            return False
        matches_attestation = (
            experiment.commit == attestation.baseline_commit
            and experiment.plan.run_config_ref == attestation.evaluator_ref
            and experiment.eval.per_sample == attestation.evidence_ref
        )
        if not matches_attestation:
            return False
        evidence_metric, evidence_commit = await _load_trusted_prepare_score(
            rt.store, attestation.evidence_ref
        )
        if (
            evidence_metric != experiment.eval.primary
            or evidence_commit != experiment.commit
        ):
            return False
        rt.state.evaluator_ref = experiment.plan.run_config_ref
        return True

    async def run_validation_phase(
        self, sota_commit: str, metric: float
    ) -> ValidationResult:
        """Run the VALIDATE phase and return the validation result."""
        rt = self._runtime
        task_context = await confirmed_task_context_block(rt)
        if rt.validation_phase is not None:
            return await rt.validation_phase(sota_commit, metric)
        if rt.provider is None:
            raise RuntimeError("VALIDATE requires a registered Agent provider")
        evaluator_ref = rt.supervisor.evaluator_ref
        if evaluator_ref is None:
            raise RuntimeError("VALIDATE requires a frozen evaluator")
        final_evaluator_ref = getattr(rt.supervisor, "final_evaluator_ref", None)
        if final_evaluator_ref is None:
            # Legacy projects do not have a separate final evaluator yet. Keep
            # working but make the limitation explicit instead of silently
            # pretending the search evaluator is an unseen final test.
            final_evaluator_ref = evaluator_ref
            logger.warning(
                "VALIDATE has no final_evaluator_ref; using the search evaluator "
                "as final. Re-run PREPARE with final-evaluator support for a "
                "true unseen final test."
            )
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
            task_context=task_context,
        )
        result_ref = None
        if rt.state.validation is not None:
            candidate = rt.state.validation.get("result_ref")
            if isinstance(candidate, str):
                result_ref = candidate
        # VALIDATE 的全部意义就是在**没被搜索过的那一份**上重打一次分。
        # 候选的 argv 是冻结的，所以换靶只能靠环境变量；不换的话重跑产出的还是
        # search 行的预测，final evaluator 报 {"primary": 0.0}。
        final_features = rt.workspaces_root / "data_split" / "final_features.csv"
        deps = ValidationDeps(
            agents=rt.agents,
            git=rt.git,
            workspace=workspace,
            execution=rt.execution,
            evaluator=rt.evaluator,
            store=rt.store,
            independent_review=self.review_validation_diff,
            checkpoint=rt.supervisor.checkpoint_validation,
            publish=lambda kind, ref, data: rt.events.project_agent_event(
                "validate", kind, ref, data
            ),
        )
        options = ValidationOptions(
            timeout_s=rt.state.experiment_timeout_s,
            predict_features=final_features if final_features.is_file() else None,
        )
        return await run_validation_plan(
            input=frozen,
            deps=deps,
            options=options,
            result_ref=result_ref,
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

"""Narrow one-Agent PREPARE phase execution."""

import asyncio
import json
import logging
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.core.workspace import GitWorkBranch, GitWorkspace, GitWorkspaceError
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.evaluation.spec import read_eval_handoff
from athena.research.supervisor.events import wait_run_events
from athena.research.supervisor.experiment import PlanRunner, load_agent_result
from athena.research.supervisor.plans import PlanDecision, PlanInput, PlanState
from athena.research.supervisor.prompt_context import handoff_block

PREPARE_AGENT_ID = "prepare"
PREPARE_PLAN_ID = "prepare"
_REAP_TIMEOUT_SECONDS = 5.0
_FEEDBACK_LIMIT = 1000
logger = logging.getLogger(__name__)
_BaselineGuard = Callable[[], Awaitable[None]]


class PrepareResult(BaseModel):
    """Trusted baseline data returned to the single-writer Supervisor."""

    model_config = ConfigDict(extra="forbid", strict=True)

    evaluator_ref: ArtifactRef
    metric: float = Field(allow_inf_nan=False)
    commit: CommitHash
    predictions_ref: ArtifactRef
    evidence_ref: ArtifactRef
    report_ref: ArtifactRef


class _BaselineGuardAbort(BaseException):
    """Carry a typed guard failure through PlanRunner's evaluator mapping."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


class _GuardedEvaluator:
    """Check baseline evidence at the last boundary before trusted scoring."""

    __slots__ = ("_assert_baseline", "_evaluator")

    def __init__(
        self, evaluator: TrustedEvaluator, assert_baseline: _BaselineGuard
    ) -> None:
        self._evaluator = evaluator
        self._assert_baseline = assert_baseline

    async def score(self, **kwargs):
        """Check authority evidence at the final trusted-scoring boundary."""
        try:
            await self._assert_baseline()
        except Exception as error:
            raise _BaselineGuardAbort(error) from error
        return await self._evaluator.score(**kwargs)


@dataclass(frozen=True, slots=True)
class _PrepareDependencies:
    agents: AgentRuntime
    evaluator: TrustedEvaluator
    git: GitWorkspace
    workspace: GitWorkBranch
    execution: ExecutionRuntime
    store: ArtifactStore
    publish: EmitEvent | None


@dataclass(frozen=True, slots=True)
class _PrepareRequest:
    evaluator_ref: ArtifactRef
    tree_ref: ArtifactRef
    task: str
    max_turns: int
    assert_baseline: _BaselineGuard
    predict_features: Path | None = None


async def _reap_agent(agents: AgentRuntime, agent_id: str) -> None:
    """Release the one-shot PREPARE agent within a short cleanup budget."""
    task = asyncio.create_task(agents.reap(agent_id))
    done, _ = await asyncio.wait({task}, timeout=_REAP_TIMEOUT_SECONDS)
    if task not in done:
        task.add_done_callback(_consume_reap_result)
        task.cancel()
        logger.warning("timed out reaping PREPARE Agent %s", agent_id)
        return
    if task.cancelled():
        logger.warning("PREPARE Agent reap was cancelled for %s", agent_id)
        return
    error = task.exception()
    if error is not None:
        logger.warning(
            "failed to reap PREPARE Agent %s",
            agent_id,
            exc_info=(type(error), error, error.__traceback__),
        )


def _consume_reap_result(task: "asyncio.Task[None]") -> None:
    if not task.cancelled():
        task.exception()


def _feedback_from_exception(error: BaseException | str) -> str:
    return " ".join(str(error).split())[:_FEEDBACK_LIMIT]


async def _decision_from_summary(summary, store: ArtifactStore) -> PlanDecision:
    decision = await load_agent_result(summary, store, PlanDecision)
    if decision is None:
        raise RuntimeError(summary.error or "prepare Agent run failed")
    return decision


async def _create_prepare_run(
    deps: _PrepareDependencies,
    request: _PrepareRequest,
    context_ref: ArtifactRef,
) -> str:
    handoff = await read_eval_handoff(deps.store, request.evaluator_ref)
    content = request.task + handoff_block(handoff)
    agent_id, run_id = await deps.agents.create_root(
        PREPARE_AGENT_ID,
        {"content": content, "context_refs": [context_ref]},
        agent_id=PREPARE_AGENT_ID,
        name=PREPARE_PLAN_ID,
    )
    if agent_id != PREPARE_AGENT_ID:
        raise RuntimeError(f"prepare Agent id must be {PREPARE_AGENT_ID}")
    return run_id


def _prepare_plan_state(
    context_ref: ArtifactRef, turn: int, max_turns: int
) -> PlanState:
    return PlanState(
        kind="PREPARE",
        context_ref=context_ref,
        turns_used=turn,
        turn_limit=max_turns,
    )


def _prepare_plan_input(request: _PrepareRequest) -> PlanInput:
    return PlanInput(evaluator_ref=request.evaluator_ref, tree_ref=request.tree_ref)


def _assert_scored_outcome(
    outcome,
    decision: str,
) -> tuple[float, CommitHash, ArtifactRef, ArtifactRef, ArtifactRef]:
    if outcome.kind == "evaluator_infrastructure_failed":
        raise RuntimeError(outcome.error or "evaluator unavailable")
    if outcome.kind != "scored":
        raise ValueError(outcome.error or f"PREPARE validation failed: {outcome.kind}")
    if outcome.commit is None:
        raise ValueError("trusted commit is missing")
    if outcome.predictions_ref is None:
        raise ValueError("trusted predictions are missing")
    if outcome.evidence_ref is None:
        raise ValueError("trusted evidence is missing")
    if outcome.report_ref is None:
        raise ValueError("trusted report is missing")
    if outcome.metric is None:
        raise ValueError("trusted metric is missing")
    if decision != "submit":
        raise ValueError(
            "baseline validated successfully "
            f"(metric {outcome.metric:.4f}) but the decision was "
            f"{decision!r}. Return submit to advance to SEARCH; "
            "continue means keep repairing this baseline, not move on."
        )
    return (
        outcome.metric,
        outcome.commit,
        outcome.predictions_ref,
        outcome.evidence_ref,
        outcome.report_ref,
    )


async def run_prepare_plan(
    *,
    agents: AgentRuntime,
    evaluator: TrustedEvaluator,
    git: GitWorkspace,
    workspace: GitWorkBranch,
    execution: ExecutionRuntime,
    store: ArtifactStore,
    evaluator_ref: ArtifactRef,
    tree_ref: ArtifactRef,
    task: str,
    max_turns: int,
    assert_baseline: _BaselineGuard,
    publish: EmitEvent | None = None,
    predict_features: Path | None = None,
) -> PrepareResult:
    """Run and repair one stable PREPARE Agent until a trusted baseline exists."""
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")

    deps = _PrepareDependencies(
        agents=agents,
        evaluator=evaluator,
        git=git,
        workspace=workspace,
        execution=execution,
        store=store,
        publish=publish,
    )
    request = _PrepareRequest(
        evaluator_ref=evaluator_ref,
        tree_ref=tree_ref,
        task=task,
        max_turns=max_turns,
        assert_baseline=assert_baseline,
        predict_features=predict_features,
    )

    deps.execution.ensure_environment()
    root = Path(workspace.path).resolve()
    root.mkdir(parents=True, exist_ok=True)

    context_ref = await store.put_text(
        json.dumps(
            {"plan_id": PREPARE_PLAN_ID, "task": task, "tree_ref": tree_ref},
            ensure_ascii=False,
        )
    )
    run_id = await _create_prepare_run(deps, request, context_ref)

    runner = PlanRunner(
        execution=execution,
        store=store,
        evaluator=_GuardedEvaluator(evaluator, assert_baseline),
        workspace=git,
        branch=workspace,
        context=ExecutionContext(
            project_root=execution.project_root,
            workspace_root=root,
            environment_root=execution.environment_root,
            experiment_id=PREPARE_PLAN_ID,
            predict_features=predict_features,
        ),
    )

    feedback: str | None = None
    try:
        for turn in range(max_turns):
            if turn:
                run_id = await agents.followup(
                    PREPARE_AGENT_ID,
                    {"content": feedback, "context_refs": []},
                )

            summary = await wait_run_events(agents, run_id, publish)
            await assert_baseline()
            try:
                decision = await _decision_from_summary(summary, store)
            except (OSError, RuntimeError, ValueError) as error:
                feedback = (
                    "previous Agent turn did not produce a valid decision: "
                    f"{_feedback_from_exception(error)}"
                )
                continue
            if decision.decision == "abandon":
                raise RuntimeError(f"prepare Agent abandoned Plan: {decision.reason}")

            try:
                outcome = await runner.run_turn(
                    PREPARE_PLAN_ID,
                    _prepare_plan_state(context_ref, turn, max_turns),
                    _prepare_plan_input(request),
                    emit=publish,
                )
                metric, commit, predictions_ref, evidence_ref, report_ref = (
                    _assert_scored_outcome(outcome, decision.decision)
                )
                return PrepareResult(
                    evaluator_ref=evaluator_ref,
                    metric=metric,
                    commit=commit,
                    predictions_ref=predictions_ref,
                    evidence_ref=evidence_ref,
                    report_ref=report_ref,
                )
            except _BaselineGuardAbort as error:
                if isinstance(error.error, RuntimeError):
                    raise error.error
                raise RuntimeError("baseline evidence guard failed") from error.error
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
                GitWorkspaceError,
            ) as error:
                feedback = _feedback_from_exception(error)
                continue
    finally:
        await _reap_agent(agents, PREPARE_AGENT_ID)

    raise RuntimeError("prepare turn budget exhausted without a trusted baseline")


__all__ = [
    "PREPARE_AGENT_ID",
    "PREPARE_PLAN_ID",
    "PrepareResult",
    "run_prepare_plan",
]

"""Narrow one-Agent PREPARE phase execution."""

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.core.workspace import (
    GitWorkBranch,
    GitWorkspace,
    GitWorkspaceError,
)
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.contracts import EvaluatorDescriptor
from athena.research.evaluation import TrustedEvaluator
from athena.research.supervisor.events import wait_run_events
from athena.research.supervisor.experiment import PlanRunner, load_agent_result
from athena.research.supervisor.plans import (
    PlanDecision,
    PlanInput,
    PlanState,
)
from athena.research.supervisor.prompt_context import handoff_block

PREPARE_AGENT_ID = "prepare"
PREPARE_PLAN_ID = "prepare"
_REAP_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)


class PrepareResult(BaseModel):
    """Trusted baseline data returned to the single-writer Supervisor."""

    model_config = ConfigDict(extra="forbid", strict=True)

    evaluator_ref: ArtifactRef
    metric: float = Field(allow_inf_nan=False)
    commit: CommitHash
    predictions_ref: ArtifactRef
    evidence_ref: ArtifactRef
    report_ref: ArtifactRef


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


async def _read_eval_handoff(
    store: ArtifactStore, evaluator_ref: ArtifactRef | None
) -> str:
    """Read the frozen evaluator handoff used by the PREPARE agent."""
    if evaluator_ref is None:
        return ""
    try:
        descriptor = EvaluatorDescriptor.model_validate_json(
            await store.get_text(evaluator_ref)
        )
        return (Path(descriptor.dir_path) / "HANDOFF.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


async def _decision_from_summary(summary, store: ArtifactStore) -> PlanDecision:
    decision = await load_agent_result(summary, store, PlanDecision)
    if decision is None:
        raise RuntimeError(summary.error or "prepare Agent run failed")
    return decision


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
    publish: EmitEvent | None = None,
    predict_features: Path | None = None,
) -> PrepareResult:
    """Run and repair one stable PREPARE Agent until a trusted baseline exists.

    ``evaluator_ref`` 是步骤 1 冻结的评估器 bundle；本步骤只产出 experiment 侧
    产物（experiment.json/solution/predictions/report/handoff）并用它可信打分。
    """

    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    root = Path(workspace.path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 初始化共享环境根（补 pyproject.toml），否则 agent 的
    # `uv add --project "$ATHENA_ENV_ROOT"` 会因缺 pyproject 失败，退到本地 venv，
    # 确定性 runner 的裸 python 再解析到无依赖解释器 → ModuleNotFoundError 死循环。
    execution.ensure_environment()

    context_ref = await store.put_text(
        json.dumps(
            {
                "plan_id": PREPARE_PLAN_ID,
                "task": task,
                "tree_ref": tree_ref,
            },
            ensure_ascii=False,
        )
    )
    # 基线也在写 predictions/，所以它必须先知道评估器要什么格式。不给的话它只能瞎猜
    # 列名和行集合——真机上就交出了 ``sample_id,probability,label_true`` 覆盖全部 6000
    # 行，而评估器要 ``__athena_row_id`` 与 1200 行留出集，直接判 0.0。
    # 契约拼进 content：context_refs 到不了 model（见 ``handoff_block``）。
    content = task + handoff_block(await _read_eval_handoff(store, evaluator_ref))
    agent_id, run_id = await agents.create_root(
        "prepare",
        {"content": content, "context_refs": [context_ref]},
        agent_id=PREPARE_AGENT_ID,
        name=PREPARE_PLAN_ID,
    )
    if agent_id != PREPARE_AGENT_ID:
        raise RuntimeError(f"prepare Agent id must be {PREPARE_AGENT_ID}")

    feedback: str | None = None
    try:
        for turn in range(max_turns):
            if turn:
                run_id = await agents.followup(
                    PREPARE_AGENT_ID,
                    {"content": feedback, "context_refs": []},
                )
            summary = await wait_run_events(agents, run_id, publish)
            try:
                decision = await _decision_from_summary(summary, store)
            except (OSError, RuntimeError, ValueError) as exc:
                # Agent 输出无效（如结构化 PlanDecision 连续重试失败/流错误）→
                # 转为反馈重试，不中断 PREPARE；真实 abort 由 decision == abandon 处理。
                feedback = (
                    "previous Agent turn did not produce a valid decision: "
                    f"{' '.join(str(exc).split())[:1000]}"
                )
                continue
            if decision.decision == "abandon":
                raise RuntimeError(f"prepare Agent abandoned Plan: {decision.reason}")
            try:
                runner = PlanRunner(
                    execution=execution,
                    store=store,
                    evaluator=evaluator,
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
                outcome = await runner.run_turn(
                    PREPARE_PLAN_ID,
                    PlanState(
                        kind="PREPARE",
                        context_ref=context_ref,
                        turns_used=turn,
                        turn_limit=max_turns,
                    ),
                    PlanInput(
                        evaluator_ref=evaluator_ref,
                        tree_ref=tree_ref,
                        initial_turn_limit=max_turns,
                    ),
                    emit=publish,
                )
                if outcome.kind == "evaluator_infrastructure_failed":
                    raise RuntimeError(outcome.error or "evaluator unavailable")
                if outcome.kind != "scored":
                    raise ValueError(
                        outcome.error or f"PREPARE validation failed: {outcome.kind}"
                    )
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
                if decision.decision != "submit":
                    raise ValueError(
                        "baseline validated successfully "
                        f"(metric {outcome.metric:.4f}) but the decision was "
                        f"{decision.decision!r}. Return submit to advance to SEARCH; "
                        "continue means keep repairing this baseline, not move on."
                    )
                return PrepareResult(
                    evaluator_ref=evaluator_ref,
                    metric=outcome.metric,
                    commit=outcome.commit,
                    predictions_ref=outcome.predictions_ref,
                    evidence_ref=outcome.evidence_ref,
                    report_ref=outcome.report_ref,
                )
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
                GitWorkspaceError,
            ) as exc:
                # 覆盖可信打分后的 git diff/commit 失败（GitWorkspaceError）与
                # subprocess 失败，转成同 Plan 的反馈重试；evaluator_infrastructure_failed
                # 仍走 RuntimeError 上抛（终端），由 Supervisor.start 统一观测，不在此处吞掉。
                feedback = " ".join(str(exc).split())[:1000]

        raise RuntimeError("prepare turn budget exhausted without a trusted baseline")
    finally:
        # The prepare Agent is a one-shot PREPARE worker; release it after the
        # phase succeeds or exhausts its turn budget.
        await _reap_agent(agents, PREPARE_AGENT_ID)


__all__ = [
    "PREPARE_AGENT_ID",
    "PREPARE_PLAN_ID",
    "PrepareResult",
    "run_prepare_plan",
]

"""Narrow one-Agent PREPARE phase execution."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.core.workspace import GitWorkBranch, GitWorkspace
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import BundleMetadata, DataScriptRunner
from athena.research.supervisor.experiment import (
    ExperimentManifest,
    PlanRunner,
    load_agent_result,
    read_experiment_manifest,
)
from athena.research.supervisor.plans import (
    PlanDecision,
    PlanInput,
    PlanState,
    wait_run_events,
)

PREPARE_AGENT_ID = "prepare"
PREPARE_PLAN_ID = "prepare"


class PrepareResult(BaseModel):
    """Trusted baseline data returned to the single-writer Supervisor."""

    model_config = ConfigDict(extra="forbid", strict=True)

    evaluator_ref: ArtifactRef
    metric: float = Field(allow_inf_nan=False)
    commit: CommitHash
    predictions_ref: ArtifactRef
    evidence_ref: ArtifactRef
    report_ref: ArtifactRef


def _workspace_output(root: Path, rel: str) -> Path:
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"output path escapes workspace: {rel}")
    return candidate


async def _freeze_evaluator(
    *,
    root: Path,
    manifest: ExperimentManifest,
    scripts: DataScriptRunner,
    store: ArtifactStore,
) -> ArtifactRef:
    evaluator_rel = manifest.outputs.get("evaluator")
    if evaluator_rel is None:
        raise ValueError("evaluator draft is missing")
    evaluator_path = _workspace_output(root, evaluator_rel)
    if evaluator_path.is_dir():
        # manifest 声明的是 evaluator 目录；入口文件约定为 evaluate.py。
        evaluator_root = evaluator_path
        entrypoint = "evaluate.py"
        if not (evaluator_root / entrypoint).is_file():
            raise ValueError(
                "outputs.evaluator directory must contain an entrypoint file "
                f"named evaluate.py: {evaluator_rel!r}"
            )
    elif evaluator_path.is_file():
        evaluator_root = evaluator_path.parent
        entrypoint = evaluator_path.name
    else:
        raise ValueError("evaluator draft is missing")
    labels_path = evaluator_root / "labels.csv"
    if not labels_path.is_file() or not labels_path.stat().st_size:
        raise ValueError("evaluator labels are missing")
    bundle = await scripts.freeze(evaluator_root, BundleMetadata(entrypoint=entrypoint))
    return await store.put_text(bundle.model_dump_json())


async def _decision_from_summary(summary, store: ArtifactStore) -> PlanDecision:
    decision = await load_agent_result(summary, store, PlanDecision)
    if decision is None:
        raise RuntimeError(summary.error or "prepare Agent run failed")
    return decision


async def run_prepare_plan(
    *,
    agents: AgentRuntime,
    scripts: DataScriptRunner,
    evaluator: TrustedEvaluator,
    git: GitWorkspace,
    workspace: GitWorkBranch,
    execution: ExecutionRuntime,
    store: ArtifactStore,
    tree_ref: ArtifactRef,
    task: str,
    max_turns: int,
    publish: EmitEvent | None = None,
) -> PrepareResult:
    """Run and repair one stable PREPARE Agent until a trusted baseline exists."""

    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    root = Path(workspace.path).resolve()
    root.mkdir(parents=True, exist_ok=True)

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
    agent_id, run_id = await agents.create_root(
        "prepare",
        {"content": task, "context_refs": [context_ref]},
        agent_id=PREPARE_AGENT_ID,
        name=PREPARE_PLAN_ID,
    )
    if agent_id != PREPARE_AGENT_ID:
        raise RuntimeError(f"prepare Agent id must be {PREPARE_AGENT_ID}")

    feedback: str | None = None
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
            manifest = read_experiment_manifest(root)
            evaluator_ref = await _freeze_evaluator(
                root=root, manifest=manifest, scripts=scripts, store=store
            )
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
                raise ValueError("Agent must submit the validated baseline")
            return PrepareResult(
                evaluator_ref=evaluator_ref,
                metric=outcome.metric,
                commit=outcome.commit,
                predictions_ref=outcome.predictions_ref,
                evidence_ref=outcome.evidence_ref,
                report_ref=outcome.report_ref,
            )
        except (OSError, ValueError) as exc:
            feedback = " ".join(str(exc).split())[:1000]

    raise RuntimeError("prepare turn budget exhausted without a trusted baseline")


__all__ = ["PREPARE_AGENT_ID", "PREPARE_PLAN_ID", "PrepareResult", "run_prepare_plan"]

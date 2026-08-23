"""Narrow one-Agent PREPARE phase execution."""

import csv
import json
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
    resolve_workspace_path,
)
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.rubrics.evaluation import normalize_metric_name
from athena.research.rubrics.models import EvaluationPolicy
from athena.research.script_runner import BundleMetadata, DataScriptRunner
from athena.research.supervisor.experiment import (
    PlanRunner,
    handoff_block,
    load_agent_result,
    read_eval_handoff,
)
from athena.research.supervisor.plans import (
    PlanDecision,
    PlanInput,
    PlanState,
    wait_run_events,
)

PREPARE_AGENT_ID = "prepare"
PREPARE_PLAN_ID = "prepare"
EVALUATOR_AGENT_ID = "evaluator"
EVALUATOR_PLAN_ID = "evaluator"


class PrepareResult(BaseModel):
    """Trusted baseline data returned to the single-writer Supervisor."""

    model_config = ConfigDict(extra="forbid", strict=True)

    evaluator_ref: ArtifactRef
    metric: float = Field(allow_inf_nan=False)
    commit: CommitHash
    predictions_ref: ArtifactRef
    evidence_ref: ArtifactRef
    report_ref: ArtifactRef


ROW_ID_COLUMN = "__athena_row_id"


def _require_joinable_labels(labels_file: Path) -> None:
    """``labels.csv`` 必须带 id 列，否则预测与标签只能按位置对齐。

    真实跑测（2026-08-16）：agent 交上来的 labels.csv 只有一列 ``label``（1200 行
    留出集），候选交的是 6000 行 ``row_id,probability``，而 evaluate.py 把两边截到较
    短长度后逐位比较。**每个候选都恒定得到 AUC≈0.502**——同一份预测按 row_id 正确
    join 是 0.8668。SEARCH 于是跑完全程、给出自信而无意义的判决。

    没有 id 列时 join 在结构上就不可能，因此这是能在冻结前静态判掉的必要条件。它不
    充分：带了 id 列仍可以写成按位置对齐。那一层由 evaluator prompt 的 shuffle 自检
    负责，运行期的判别性检查见 docs/evaluator_contract_ch.md。
    """
    with labels_file.open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle), [])
    columns = [name.strip() for name in header if name.strip()]
    if len(columns) < 2:
        raise ValueError(
            f"labels.csv must carry a row-id column named {ROW_ID_COLUMN!r} next to "
            f"the target so predictions can be joined by id, but its header is "
            f"{columns or ['<empty>']}. Rewrite it as "
            f"'{ROW_ID_COLUMN},<target>' and make evaluate.py join on that column "
            "instead of comparing the two files row by row."
        )


async def _freeze_evaluator(
    *,
    root: Path,
    scripts: DataScriptRunner,
    store: ArtifactStore,
    evaluation_policy: EvaluationPolicy | None = None,
) -> ArtifactRef:
    """Freeze the evaluator directory (metric.json's eval_script) into a bundle.

    Labels may be a ``labels.csv`` file or a non-empty ``labels/`` directory.
    ``freeze`` walks the whole evaluator directory (``rglob("*")``), so an
    ``evaluator/HANDOFF.md`` — the self-describing eval spec — is bundled
    alongside the evaluator code for SEARCH to read.
    """
    spec_path = root / "metric.json"
    if not spec_path.is_file():
        raise ValueError("metric.json is missing")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        evaluator_rel = spec["eval_script"]
    except (OSError, ValueError, KeyError):
        raise ValueError("metric.json must declare eval_script")
    if evaluation_policy is not None:
        try:
            declared_metric = normalize_metric_name(spec["primary_metric"])
            declared_direction = spec["direction"]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError(
                "metric.json must declare primary_metric and direction from the "
                "frozen Evaluation Policy"
            ) from None
        if declared_metric != evaluation_policy.primary_metric:
            raise ValueError(
                "evaluator primary_metric does not match frozen Evaluation Policy: "
                f"{declared_metric!r} != {evaluation_policy.primary_metric!r}"
            )
        if declared_direction != evaluation_policy.direction:
            raise ValueError(
                "evaluator direction does not match frozen Evaluation Policy: "
                f"{declared_direction!r} != {evaluation_policy.direction!r}"
            )
    try:
        evaluator_path = resolve_workspace_path(root, evaluator_rel)
    except ValueError as exc:
        raise ValueError(f"output path escapes workspace: {evaluator_rel}") from exc
    if evaluator_path.is_dir():
        # eval_script 声明的是目录；入口文件约定为 evaluate.py。
        evaluator_root = evaluator_path
        entrypoint = "evaluate.py"
        if not (evaluator_root / entrypoint).is_file():
            raise ValueError(
                "eval_script directory must contain an entrypoint file "
                f"named evaluate.py: {evaluator_rel!r}"
            )
    elif evaluator_path.is_file():
        evaluator_root = evaluator_path.parent
        entrypoint = evaluator_path.name
    else:
        raise ValueError("eval_script is missing")
    labels_file = evaluator_root / "labels.csv"
    labels_dir = evaluator_root / "labels"
    if not (
        (labels_file.is_file() and labels_file.stat().st_size)
        or (labels_dir.is_dir() and any(p.is_file() for p in labels_dir.rglob("*")))
    ):
        raise ValueError(
            "eval labels are missing: labels.csv or a non-empty labels/ dir must "
            f"sit next to the eval script (same directory as {evaluator_rel!r})"
        )
    if labels_file.is_file() and labels_file.stat().st_size:
        _require_joinable_labels(labels_file)
    bundle = await scripts.freeze(evaluator_root, BundleMetadata(entrypoint=entrypoint))
    return await store.put_text(bundle.model_dump_json())


async def _decision_from_summary(summary, store: ArtifactStore) -> PlanDecision:
    decision = await load_agent_result(summary, store, PlanDecision)
    if decision is None:
        raise RuntimeError(summary.error or "prepare Agent run failed")
    return decision


async def run_evaluator_plan(
    *,
    agents: AgentRuntime,
    scripts: DataScriptRunner,
    store: ArtifactStore,
    evaluator_dir: Path,
    execution: ExecutionRuntime,
    task: str,
    evaluation_policy: EvaluationPolicy | None = None,
    max_turns: int,
    publish: EmitEvent | None = None,
) -> ArtifactRef:
    """Run and repair one evaluator Agent until a frozen evaluator bundle exists."""

    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    root = Path(evaluator_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 与 experiment 步骤一样，先补环境根 pyproject.toml，避免 agent 的
    # `uv add --project "$ATHENA_ENV_ROOT"` 因缺 pyproject 失败。
    execution.ensure_environment()

    policy_payload = (
        evaluation_policy.model_dump(mode="json")
        if evaluation_policy is not None
        else None
    )
    context_ref = await store.put_text(
        json.dumps(
            {
                "plan_id": EVALUATOR_PLAN_ID,
                "task": task,
                "evaluation_policy": policy_payload,
            },
            ensure_ascii=False,
        )
    )
    content = task
    if policy_payload is not None:
        content += (
            "\n\nFROZEN RESEARCH EVALUATION POLICY (authoritative; do not "
            "reselect the primary metric):\n"
            + json.dumps(policy_payload, ensure_ascii=False, indent=2)
        )
    agent_id, run_id = await agents.create_root(
        "evaluator",
        {"content": content, "context_refs": [context_ref]},
        agent_id=EVALUATOR_AGENT_ID,
        name=EVALUATOR_PLAN_ID,
    )
    if agent_id != EVALUATOR_AGENT_ID:
        raise RuntimeError(f"evaluator Agent id must be {EVALUATOR_AGENT_ID}")

    feedback: str | None = None
    for turn in range(max_turns):
        if turn:
            run_id = await agents.followup(
                EVALUATOR_AGENT_ID,
                {"content": feedback, "context_refs": []},
            )
        summary = await wait_run_events(agents, run_id, publish)
        try:
            decision = await _decision_from_summary(summary, store)
        except (OSError, RuntimeError, ValueError) as exc:
            # Agent 输出无效 → 转为反馈重试；真实 abort 由 decision == abandon 处理。
            feedback = (
                "previous evaluator turn did not produce a valid decision: "
                f"{' '.join(str(exc).split())[:1000]}"
            )
            continue
        if decision.decision == "abandon":
            raise RuntimeError(f"evaluator Agent abandoned Plan: {decision.reason}")
        try:
            evaluator_ref = await _freeze_evaluator(
                root=root,
                scripts=scripts,
                store=store,
                evaluation_policy=evaluation_policy,
            )
            if decision.decision != "submit":
                raise ValueError(
                    "evaluator frozen successfully but the decision was "
                    f"{decision.decision!r}. Return submit to advance to the "
                    "experiment step."
                )
            return evaluator_ref
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # 覆盖 evaluator 冻结（uv lock）的子进程失败 → 转成同 Plan 的反馈重试。
            feedback = " ".join(str(exc).split())[:1000]

    raise RuntimeError("evaluator turn budget exhausted without a frozen evaluator")


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
    content = task + handoff_block(await read_eval_handoff(store, evaluator_ref))
    agent_id, run_id = await agents.create_root(
        "prepare",
        {"content": content, "context_refs": [context_ref]},
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


__all__ = [
    "PREPARE_AGENT_ID",
    "PREPARE_PLAN_ID",
    "EVALUATOR_AGENT_ID",
    "EVALUATOR_PLAN_ID",
    "PrepareResult",
    "run_evaluator_plan",
    "run_prepare_plan",
]

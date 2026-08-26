"""Narrow one-Agent PREPARE phase execution."""

import csv
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from pydantic import BaseModel, ConfigDict, Field

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.research.contracts import DataScriptBundle
from athena.research.evaluator_trust import validate_evaluator_properties
from athena.core.workspace import (
    GitWorkBranch,
    GitWorkspace,
    GitWorkspaceError,
    resolve_workspace_path,
)
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
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
FINAL_EVALUATOR_AGENT_ID = "final_evaluator"
FINAL_EVALUATOR_PLAN_ID = "final_evaluator"


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
        reader = csv.DictReader(handle)
        columns = [name.strip() for name in (reader.fieldnames or []) if name.strip()]
        if ROW_ID_COLUMN not in columns or len(columns) < 2:
            raise ValueError(
                f"labels.csv must carry a row-id column named {ROW_ID_COLUMN!r} next to "
                f"the target so predictions can be joined by id, but its header is "
                f"{columns or ['<empty>']}. Rewrite it as "
                f"'{ROW_ID_COLUMN},<target>' and make evaluate.py join on that column "
                "instead of comparing the two files row by row."
            )
        seen: set[str] = set()
        for row in reader:
            raw = (row.get(ROW_ID_COLUMN) or "").strip()
            if not raw:
                raise ValueError(
                    f"labels.csv contains an empty {ROW_ID_COLUMN!r}"
                )
            if raw in seen:
                raise ValueError(
                    f"labels.csv contains duplicate {ROW_ID_COLUMN!r}: {raw}"
                )
            seen.add(raw)


def _require_joinable_labels_dir(labels_dir: Path) -> None:
    """Require every ``*.csv`` under ``labels/`` to carry the exact id column."""
    csv_files = sorted(labels_dir.rglob("*.csv"))
    if not csv_files:
        raise ValueError(
            f"labels/ must contain at least one .csv file carrying "
            f"{ROW_ID_COLUMN!r}"
        )
    for path in csv_files:
        _require_joinable_labels(path)


async def _freeze_evaluator(
    *,
    root: Path,
    scripts: DataScriptRunner,
    store: ArtifactStore,
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
    elif labels_dir.is_dir():
        _require_joinable_labels_dir(labels_dir)
    bundle = await scripts.freeze(evaluator_root, BundleMetadata(entrypoint=entrypoint))
    return await store.put_text(bundle.model_dump_json())


async def _decision_from_summary(summary, store: ArtifactStore) -> PlanDecision:
    decision = await load_agent_result(summary, store, PlanDecision)
    if decision is None:
        raise RuntimeError(summary.error or "prepare Agent run failed")
    return decision


async def _validate_frozen_evaluator(
    *,
    root: Path,
    evaluator_ref: ArtifactRef,
    scripts: DataScriptRunner,
    store: ArtifactStore,
) -> None:
    """Run deterministic row-order/value-permutation property tests.

    A frozen evaluator that fails these checks is not trustworthy and should be
    sent back to the agent for repair. Runners without ``run`` (test doubles)
    skip the behavioral tests with a warning.
    """
    labels_file = root / "labels.csv"
    labels_dir = root / "labels"
    if labels_file.is_file():
        labels_csv = labels_file.read_text(encoding="utf-8-sig")
    elif labels_dir.is_dir():
        csv_files = sorted(labels_dir.rglob("*.csv"))
        if not csv_files:
            raise ValueError("labels/ has no csv for evaluator property tests")
        labels_csv = csv_files[0].read_text(encoding="utf-8-sig")
    else:
        raise ValueError("no labels found for evaluator property tests")

    async def score(predictions_csv: str) -> float:
        bundle = DataScriptBundle.model_validate_json(
            await store.get_text(evaluator_ref)
        )
        result = await scripts.run(
            bundle,
            request={},
            extra_files={
                "predictions/predictions.csv": predictions_csv.encode("utf-8")
            },
            output_schema={"primary": None},
        )
        return float(result.outputs["primary"])

    try:
        outcome = await validate_evaluator_properties(labels_csv, score)
    except AttributeError:
        logger.warning("evaluator property tests skipped: runner has no run()")
        return
    if not outcome.get("ok"):
        raise ValueError(outcome.get("reason", "evaluator property tests failed"))


async def run_evaluator_plan(
    *,
    agents: AgentRuntime,
    scripts: DataScriptRunner,
    store: ArtifactStore,
    evaluator_dir: Path,
    execution: ExecutionRuntime,
    task: str,
    max_turns: int,
    publish: EmitEvent | None = None,
    agent_id: str = EVALUATOR_AGENT_ID,
    plan_id: str = EVALUATOR_PLAN_ID,
) -> ArtifactRef:
    """Run and repair one evaluator Agent until a frozen evaluator bundle exists."""

    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    root = Path(evaluator_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 与 experiment 步骤一样，先补环境根 pyproject.toml，避免 agent 的
    # `uv add --project "$ATHENA_ENV_ROOT"` 因缺 pyproject 失败。
    execution.ensure_environment()

    context_ref = await store.put_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "task": task,
            },
            ensure_ascii=False,
        )
    )
    agent_id, run_id = await agents.create_root(
        "evaluator",
        {"content": task, "context_refs": [context_ref]},
        agent_id=agent_id,
        name=plan_id,
    )

    feedback: str | None = None
    try:
        for turn in range(max_turns):
            if turn:
                run_id = await agents.followup(
                    agent_id,
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
                    root=root, scripts=scripts, store=store
                )
                if decision.decision != "submit":
                    raise ValueError(
                        "evaluator frozen successfully but the decision was "
                        f"{decision.decision!r}. Return submit to advance to the "
                        "experiment step."
                    )
                await _validate_frozen_evaluator(
                    root=root,
                    evaluator_ref=evaluator_ref,
                    scripts=scripts,
                    store=store,
                )
                return evaluator_ref
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # 覆盖 evaluator 冻结（uv lock）的子进程失败 → 转成同 Plan 的反馈重试。
                feedback = " ".join(str(exc).split())[:1000]

        raise RuntimeError("evaluator turn budget exhausted without a frozen evaluator")
    finally:
        # The evaluator Agent is a one-shot PREPARE worker; release it after the
        # phase succeeds or exhausts its turn budget.
        try:
            await agents.reap(agent_id)
        except Exception:  # noqa: BLE001,S110 - GC must never mask PREPARE failure
            pass


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
        try:
            await agents.reap(PREPARE_AGENT_ID)
        except Exception:  # noqa: BLE001,S110 - GC must never mask PREPARE failure
            pass


__all__ = [
    "EVALUATOR_AGENT_ID",
    "EVALUATOR_PLAN_ID",
    "PREPARE_AGENT_ID",
    "PREPARE_PLAN_ID",
    "PrepareResult",
    "run_evaluator_plan",
    "run_prepare_plan",
]

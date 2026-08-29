"""Narrow one-Agent PREPARE phase execution."""

import csv
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from pydantic import BaseModel, ConfigDict, Field

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.research.contracts import EvaluatorDescriptor
from athena.research.evaluator_trust import (
    extract_prediction_column,
    extract_prediction_column_from_source,
    validate_evaluator_properties,
)
from athena.core.workspace import (
    GitWorkBranch,
    GitWorkspace,
    GitWorkspaceError,
    resolve_workspace_path,
)
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import DataScriptRunner
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
    """``labels.csv`` 必须带 id 列，否则预测与标签只能按位置对齐。"""
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
                raise ValueError(f"labels.csv contains an empty {ROW_ID_COLUMN!r}")
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


def _evaluator_layout(root: Path) -> tuple[Path, str, str]:
    """Return ``(evaluator_root, entrypoint, prediction_format)`` from metric.json.

    This is the single source of truth shared by the README freeze marker and
    the platform's format-aware validation.
    """
    spec_path = root / "metric.json"
    if not spec_path.is_file():
        # 报出绝对路径，因为最常见的失败不是"忘了写 metric.json"，而是**写到了别的
        # 目录**：agent 可以用 shell_command 的绝对路径在 workspace 外面建好整套文件，
        # 然后对着那份东西反复 submit。只说"metric.json is missing"时，它看自己刚
        # 列过的目录，文件明明都在，于是原样再交一次。
        existing = sorted(p.name for p in root.iterdir())[:10] if root.is_dir() else []
        raise ValueError(
            f"metric.json is missing from your workspace {root}. "
            f"That directory currently holds: {existing or 'nothing'}. "
            "Write every evaluator file inside that exact directory; files you "
            "created anywhere else do not count."
        )
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        evaluator_rel = spec["eval_script"]
    except (OSError, ValueError, KeyError):
        raise ValueError("metric.json must declare eval_script")
    prediction_format = spec.get("prediction_format", "tabular_csv")
    try:
        evaluator_path = resolve_workspace_path(root, evaluator_rel)
    except ValueError as exc:
        raise ValueError(f"output path escapes workspace: {evaluator_rel}") from exc
    if evaluator_path.is_dir():
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
    return evaluator_root, entrypoint, prediction_format


def _evaluator_readme(root: Path, *, entrypoint: str, prediction_format: str) -> str:
    """Build the prompt-level freeze marker for an accepted evaluator."""
    return (
        "# Evaluator Freeze Marker\n\n"
        f"This evaluator in `{root}` has been accepted.\n\n"
        "## Freeze contract\n\n"
        "- Do NOT modify `evaluate.py`, `metric.json`, labels, `HANDOFF.md`, "
        "`pyproject.toml`, or `README.md` further.\n"
        "- This directory is the authoritative evaluator for the current run.\n"
        "- If a change is required, create a new evaluator version and rerun the "
        "full acceptance flow.\n\n"
        "## Declared format\n\n"
        f"- entrypoint: `{entrypoint}`\n"
        f"- prediction_format: `{prediction_format}`\n"
        "- See `HANDOFF.md` for the precise prediction schema, identity key, "
        "held-out split, and metric definition.\n"
    )


async def _decision_from_summary(summary, store: ArtifactStore) -> PlanDecision:
    decision = await load_agent_result(summary, store, PlanDecision)
    if decision is None:
        raise RuntimeError(summary.error or "prepare Agent run failed")
    return decision


async def _validate_frozen_evaluator(
    *,
    root: Path,
    scripts: DataScriptRunner,
    store: ArtifactStore,
) -> None:
    """Run format-aware property tests on a README-only evaluator directory.

    CSV row/value probes only apply to ``prediction_format: tabular_csv``.
    Custom formats are validated by the agent's own probes described in
    ``HANDOFF.md``; this function skips CSV-only checks for them.
    """
    evaluator_root, entrypoint, prediction_format = _evaluator_layout(root)
    if prediction_format != "tabular_csv":
        logger.warning(
            "evaluator property tests skipped: prediction_format=%s",
            prediction_format,
        )
        return

    labels_file = evaluator_root / "labels.csv"
    labels_dir = evaluator_root / "labels"
    if labels_file.is_file():
        _require_joinable_labels(labels_file)
        labels_csv = labels_file.read_text(encoding="utf-8-sig")
    elif labels_dir.is_dir():
        _require_joinable_labels_dir(labels_dir)
        csv_files = sorted(labels_dir.rglob("*.csv"))
        if not csv_files:
            raise ValueError("labels/ has no csv for evaluator property tests")
        labels_csv = csv_files[0].read_text(encoding="utf-8-sig")
    else:
        raise ValueError("no labels found for evaluator property tests")

    # HANDOFF.md is the evaluator's self-declared prediction contract. Use its
    # declared CSV prediction column when present, falling back to the historical
    # "prediction" column so existing tabular evaluators keep working.
    handoff_path = evaluator_root / "HANDOFF.md"
    handoff_text = (
        handoff_path.read_text(encoding="utf-8") if handoff_path.is_file() else ""
    )
    prediction_column = extract_prediction_column(handoff_text)
    if prediction_column is None:
        # Do not depend on a prompt-specific HANDOFF wording. If the free-text
        # declaration is not parseable, inspect the evaluator source itself for
        # the CSV column it reads.
        source_path = evaluator_root / entrypoint
        source = (
            source_path.read_text(encoding="utf-8", errors="replace")
            if source_path.is_file()
            else ""
        )
        prediction_column = extract_prediction_column_from_source(source)
    if prediction_column is None:
        raise ValueError(
            "cannot determine the tabular prediction CSV column from either "
            "HANDOFF.md or the evaluator source; declare it in HANDOFF.md or "
            "make evaluate.py read a clearly named prediction column"
        )

    async def score(predictions_csv: str) -> float:
        result = await scripts.run_dir(
            evaluator_root,
            request={},
            extra_files={
                "predictions/predictions.csv": predictions_csv.encode("utf-8")
            },
            output_schema={"primary": None},
        )
        return float(result.outputs["primary"])

    try:
        outcome = await validate_evaluator_properties(
            labels_csv, score, prediction_column=prediction_column
        )
    except AttributeError:
        logger.warning("evaluator property tests skipped: runner has no run_dir()")
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
    ask_user: Any | None = None,
    agent_id: str = EVALUATOR_AGENT_ID,
    plan_id: str = EVALUATOR_PLAN_ID,
) -> ArtifactRef:
    """Run and repair one evaluator Agent until it is accepted via README."""

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
            if decision.decision != "submit":
                feedback = (
                    f"Return submit to advance; got {decision.decision!r}. "
                    "Continue means keep repairing the evaluator, not accept it."
                )
                continue
            try:
                evaluator_root, entrypoint, prediction_format = _evaluator_layout(root)
                readme_text = _evaluator_readme(
                    root,
                    entrypoint=entrypoint,
                    prediction_format=prediction_format,
                )
                (root / "README.md").write_text(readme_text, encoding="utf-8")
                readme_ref = await store.put_text(readme_text)
                evaluator_ref = await store.put_text(
                    EvaluatorDescriptor(
                        dir_path=str(root.resolve()),
                        readme_ref=readme_ref,
                        prediction_format=prediction_format,
                        entrypoint=entrypoint,
                    ).model_dump_json()
                )
                await _validate_frozen_evaluator(
                    root=root,
                    scripts=scripts,
                    store=store,
                )
                if prediction_format != "tabular_csv" and ask_user is not None:
                    answer = await ask_user(
                        "This evaluator uses a custom prediction format, so the "
                        "platform cannot run its CSV-only automated probes. "
                        "Confirm the HANDOFF.md format declaration and the agent's "
                        "format-aware probes are acceptable?",
                        choices=[
                            {"label": "接受", "value": "accept"},
                            {"label": "拒绝", "value": "reject"},
                        ],
                        allow_custom=True,
                        allow_skip=True,
                    )
                    if answer is not None:
                        lowered = str(answer).lower()
                        if "reject" in lowered or "拒绝" in answer:
                            raise ValueError(
                                "human rejected custom evaluator acceptance"
                            )
                return evaluator_ref
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # 写入 README/校验/目录运行失败 → 转成同 Plan 的反馈重试。
                feedback = " ".join(str(exc).split())[:1000]
                # 拒绝理由此前只发给 agent，操作者的日志里一个字都没有。真机
                # （2026-08-29）上 agent 连交 10 次 submit 全被拒、直到预算耗尽，
                # 而日志里只有一句"turn budget exhausted"——从外面看是无缘无故的
                # 空转，没有任何线索指向真正的原因。
                logger.warning(
                    "evaluator %s submit rejected on turn %d/%d: %s",
                    plan_id,
                    turn + 1,
                    max_turns,
                    feedback,
                )

        raise RuntimeError(
            f"evaluator turn budget exhausted without an accepted evaluator "
            f"({plan_id}); last rejection: {feedback or 'none recorded'}"
        )
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

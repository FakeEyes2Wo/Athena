"""Manifest execution, trusted scoring, and patience for one autonomous Plan.

复用既有能力：manifest 命令经 ``ExecutionRuntime`` 的 bounded argv 执行，
证据/预测经 ``LocalArtifactStore`` 存取，打分经 ``TrustedEvaluator`` 与冻结
``DataScriptBundle``，可信修订经 ``LocalGitWorkspace`` 的 review/commit 提交。
计划级状态合同来自 ``plans.py``（Task 1）。
"""

import json
import os
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from athena.core.agent.types import RunStatus
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.core.workspace import GitWorkBranch, GitWorkspace
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.contracts import DataScriptBundle
from athena.research.evaluation import TrustedEvaluator
from athena.research.supervisor.events import redact
from athena.research.supervisor.plans import (
    PlanBest,
    PlanDecision,
    PlanInput,
    PlanState,
)

Direction = Literal["maximize", "minimize"]

# manifest 禁止声明的可执行文件（平台自有，agent 不得直接调用）。
_FORBIDDEN_EXECUTABLES = frozenset({"git", "git.exe"})
_MANIFEST_FIELDS = frozenset({"version", "commands", "outputs"})


def _validate_relative_path(path: str, label: str) -> None:
    """拒绝绝对路径、驱动器相对路径、空段与 ``..`` 逃逸的 workspace 相对路径。"""
    if os.path.isabs(path):
        raise ValueError(f"{label} path must be relative to the workspace")
    # Windows 驱动器相对路径（如 "C:foo"）isabs 为 False，但会落到 C: 盘当前目录。
    if os.path.splitdrive(path)[0]:
        raise ValueError(f"{label} path must be relative to the workspace")
    parts = path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{label} path escapes the workspace")


def _manifest_validation_summary(error: ValidationError) -> str:
    """Return actionable validation details without manifest input values."""
    summaries: list[str] = []
    for detail in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(
            (
                str(part)
                if isinstance(part, int)
                else part if part in _MANIFEST_FIELDS else "<field>"
            )
            for part in detail["loc"]
        )
        message = " ".join(detail["msg"].split())
        summaries.append(f"{location}: {message}" if location else message)
    return "; ".join(summaries)[:1000]


class ExperimentManifest(BaseModel):
    """Workspace 根声明的一次实验：argv 命令与预测/报告输出路径。

    命令必须是 argv 数组（绝不能是 shell 字符串），输出路径解析在指派
    workspace 内。manifest 不能声明标签、分数、Git 命令或 workspace 路径。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    version: int
    commands: list[list[str]]
    outputs: dict[str, str]

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("only manifest version 1 is supported")
        return value

    @field_validator("commands", mode="before")
    @classmethod
    def _coerce_single_command(cls, value: object) -> object:
        # Agent 常把单条命令写成扁平数组 ["python", "x.py"]，兼容为 [["python", "x.py"]]。
        if isinstance(value, list) and value and isinstance(value[0], str):
            return [value]
        return value

    @field_validator("commands")
    @classmethod
    def _validate_commands(cls, value: list[list[str]]) -> list[list[str]]:
        for argv in value:
            if not argv or any(not part.strip() for part in argv):
                raise ValueError("each command must be a non-empty argv array")
            # 用 basename 判断，杜绝绝对路径绕过（如 /usr/bin/git、C:\\...\\git.exe）。
            if (
                os.path.basename(argv[0].replace("\\", "/")).lower()
                in _FORBIDDEN_EXECUTABLES
            ):
                raise ValueError("manifest cannot run git commands")
        return value

    @field_validator("outputs")
    @classmethod
    def _validate_outputs(cls, value: dict[str, str]) -> dict[str, str]:
        if "predictions" not in value:
            raise ValueError("predictions output is mandatory")
        for key, rel in value.items():
            _validate_relative_path(rel, f"output {key}")
        return value


def read_experiment_manifest(root: Path) -> ExperimentManifest:
    """解析并校验 workspace 根 experiment.json，返回带可行动错误摘要的 manifest。"""
    path = root / "experiment.json"
    if not path.is_file():
        raise ValueError("experiment.json is missing")
    try:
        return ExperimentManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ValueError(_manifest_validation_summary(exc)) from exc


_ModelT = TypeVar("_ModelT", bound=BaseModel)


async def load_agent_result(
    summary: Any, store: ArtifactStore, model_type: type[_ModelT]
) -> _ModelT | None:
    """解包 RunSummary.response_ref → result_ref → store → Pydantic 模型。

    仅当 status 非 COMPLETED、缺 response_ref 或缺 result_ref 时返回 None；JSON/
    存储/模型解析异常照常抛出，由调用方按各自语义处理。三处 phase 共用，避免复制
    status 判断与两层解包。
    """
    status = getattr(summary.status, "value", summary.status)
    if status != RunStatus.COMPLETED.value or summary.response_ref is None:
        return None
    response = json.loads(summary.response_ref)
    result_ref = response.get("result_ref") if isinstance(response, dict) else None
    if not isinstance(result_ref, str):
        return None
    return model_type.model_validate_json(await store.get_text(result_ref))


class PlanSettlement(BaseModel):
    """一次 turn 的终态动作：settle / wait / continue 及其依据。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["settle", "wait", "continue"]
    best_ref: ArtifactRef | None = None
    reason: str = ""


class PlanTurnResult(BaseModel):
    """一次 Plan turn 的结果：打分证据与 patience 状态。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal[
        "scored",
        "scoring_failed",
        "evaluator_infrastructure_failed",
        "output_failed",
        "execution_failed",
        "manifest_invalid",
    ]
    metric: float | None = None
    commit: CommitHash | None = None
    next_state: PlanState | None = None
    predictions_ref: ArtifactRef | None = None
    evidence_ref: ArtifactRef | None = None
    report_ref: ArtifactRef | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_next_state(self) -> "PlanTurnResult":
        if self.kind == "scored" and self.next_state is None:
            raise ValueError("scored result requires next_state")
        if self.kind != "scored" and self.next_state is not None:
            raise ValueError("failure result must not carry next_state")
        return self


def _better(candidate: float, reference: float, direction: Direction) -> bool:
    """判断 ``candidate`` 是否在指标方向上优于 ``reference``。"""
    return candidate > reference if direction == "maximize" else candidate < reference


async def load_best(best_ref: ArtifactRef, store: ArtifactStore) -> PlanBest:
    """从 artifact 引用加载不可变 PlanBest 记录。"""
    return PlanBest.model_validate_json(await store.get_text(best_ref))


async def apply_trusted_score(
    state: PlanState,
    metric: float,
    commit: CommitHash,
    *,
    store: ArtifactStore,
    evidence_ref: ArtifactRef,
    direction: Direction = "maximize",
) -> PlanState:
    """应用一次可信分数：更新不可变 best 并调整 stale_rounds。

    优于历史 best → 写新 PlanBest、best_ref 指向它、stale_rounds 归零；
    未显著改进 → stale_rounds 加一且保留历史 best。返回 ``state`` 的副本。
    """
    current = await load_best(state.best_ref, store) if state.best_ref else None
    improved = current is None or _better(metric, current.metric, direction)
    if improved:
        best = PlanBest(metric=metric, commit=commit, evidence_ref=evidence_ref)
        best_ref = await store.put_text(best.model_dump_json())
        return state.model_copy(update={"best_ref": best_ref, "stale_rounds": 0})
    return state.model_copy(update={"stale_rounds": state.stale_rounds + 1})


def decide_settlement(
    state: PlanState,
    decision: PlanDecision,
    *,
    report_ref: ArtifactRef | None = None,
) -> PlanSettlement:
    """根据 Plan 预算与 Agent 决策返回 turn 的终态动作。

    submit/abandon 一律 settle（无 best 时按 LOSS settle）；continue 下 patience
    用尽或 turn 用尽且有 best → settle best；turn 用尽且无 best → wait。
    """
    has_best = state.best_ref is not None
    patience_exhausted = (
        state.patience is not None and state.stale_rounds >= state.patience
    )
    turns_exhausted = (
        state.turn_limit is not None and state.turns_used >= state.turn_limit
    )
    settlement: PlanSettlement
    if decision.decision in ("submit", "abandon"):
        settlement = PlanSettlement(
            action="settle", best_ref=state.best_ref, reason=decision.decision
        )
    elif patience_exhausted:
        settlement = PlanSettlement(
            action="settle", best_ref=state.best_ref, reason="patience exhausted"
        )
    elif turns_exhausted:
        if has_best:
            settlement = PlanSettlement(
                action="settle",
                best_ref=state.best_ref,
                reason="turn budget exhausted",
            )
        else:
            settlement = PlanSettlement(
                action="wait", reason="turn budget exhausted without a trusted best"
            )
    else:
        settlement = PlanSettlement(action="continue")
    # 只有 PREPARE 强制要求 report；SEARCH 的 report 输出是可选的，缺失不应卡住 settle。
    if settlement.action == "settle" and state.kind == "PREPARE" and report_ref is None:
        return PlanSettlement(action="wait", reason="report required before settlement")
    return settlement


class PlanRunner:
    """执行一次 Plan turn：manifest、打分、提交与 trusted patience。

    仅依赖既有 owner：ExecutionRuntime（argv 执行）、ArtifactStore（证据）、
    TrustedEvaluator + DataScriptBundle（可信打分）、GitWorkspace（review/commit）。
    """

    def __init__(
        self,
        *,
        execution: ExecutionRuntime,
        store: ArtifactStore,
        evaluator: TrustedEvaluator,
        workspace: GitWorkspace,
        branch: GitWorkBranch,
        context: ExecutionContext,
        direction: Direction = "maximize",
        timeout_s: int = 120,
    ) -> None:
        self._execution = execution
        self._store = store
        self._evaluator = evaluator
        self._workspace = workspace
        self._branch = branch
        self._context = context
        self._direction = direction
        self._timeout_s = timeout_s

    @property
    def workdir(self) -> Path:
        """Plan workspace 根（manifest 所在目录）。"""
        return Path(self._branch.path)

    async def run_turn(
        self,
        plan_id: str,
        state: PlanState,
        plan_input: PlanInput,
        *,
        emit: EmitEvent | None = None,
    ) -> PlanTurnResult:
        """执行 workspace manifest、可信打分并提交一个修订。

        失败（manifest/执行/输出/打分）返回非 scored 结果且不触碰 patience；
        仅可信分数成功才提交修订并更新 best/stale_rounds。
        """
        try:
            manifest = read_experiment_manifest(self.workdir)
        except ValueError as exc:
            return await self._failure(
                plan_id, "manifest_invalid", " ".join(str(exc).split())[:1000]
            )

        for argv in manifest.commands:
            result = await self._execution.run(
                self._context,
                argv=argv,
                timeout_s=self._timeout_s,
                workdir=str(self.workdir),
                emit=emit,
            )
            if not result.ok:
                error = (
                    f"command failed (exit {result.exit_code}): {result.stderr[:200]}"
                )
                if "ModuleNotFoundError" in result.stderr:
                    error += (
                        ' Run "uv sync --project $ATHENA_ENV_ROOT" to install the '
                        "declared dependencies into the environment venv, then retry."
                    )
                return await self._failure(plan_id, "execution_failed", error)

        predictions_path = self.workdir / manifest.outputs["predictions"]
        if not predictions_path.is_file() or not predictions_path.stat().st_size:
            return await self._failure(
                plan_id,
                "output_failed",
                f"missing predictions output: {manifest.outputs['predictions']}",
            )
        predictions = predictions_path.read_text(encoding="utf-8")
        predictions_ref = await self._store.put_text(predictions)
        report_ref = await self._store_report(manifest)
        if state.kind == "PREPARE" and report_ref is None:
            return await self._failure(
                plan_id,
                "output_failed",
                "PREPARE requires a declared, non-empty report output",
                predictions_ref=predictions_ref,
            )

        if state.kind == "PREPARE":
            metric = await self._prepare_metric(plan_id)
            if metric is None:
                return await self._failure(
                    plan_id,
                    "scoring_failed",
                    "metric.json eval script failed or produced no primary score",
                    predictions_ref=predictions_ref,
                )
        else:
            bundle = await self._load_bundle(plan_input.evaluator_ref)
            if bundle is None:
                return await self._failure(
                    plan_id,
                    "scoring_failed",
                    "frozen evaluator artifact is invalid",
                    predictions_ref=predictions_ref,
                )
            try:
                evaluation = await self._evaluator.score(
                    eval_bundle=bundle,
                    predictions=predictions,
                    candidate_id=plan_id,
                    direction=self._direction,
                    predictions_path=manifest.outputs["predictions"],
                )
            except ValueError as exc:
                # 候选输出导致评估失败 → 同 Plan 修复，不产生可信分数
                return await self._failure(
                    plan_id,
                    "scoring_failed",
                    str(exc),
                    predictions_ref=predictions_ref,
                )
            except Exception as exc:
                return await self._failure(
                    plan_id,
                    "evaluator_infrastructure_failed",
                    str(exc),
                    predictions_ref=predictions_ref,
                )
            metric = evaluation.test_score

        diff = await self._workspace.diff(self._branch)
        commit = await self._workspace.commit(
            self._branch, diff, f"plan {plan_id} trusted score {metric:.4f}"
        )
        evidence_ref = await self._store.put_text(
            json.dumps(
                {
                    "plan": plan_id,
                    "metric": metric,
                    "commit": commit,
                    "predictions_ref": predictions_ref,
                    "report_ref": report_ref,
                    "outputs": manifest.outputs,
                },
                ensure_ascii=False,
            )
        )
        updated = state
        if state.kind == "SEARCH":
            updated = await apply_trusted_score(
                state,
                metric,
                commit,
                store=self._store,
                evidence_ref=evidence_ref,
                direction=self._direction,
            )
        return PlanTurnResult(
            kind="scored",
            metric=metric,
            commit=commit,
            next_state=updated,
            predictions_ref=predictions_ref,
            evidence_ref=evidence_ref,
            report_ref=report_ref,
        )

    async def _prepare_metric(self, plan_id: str) -> float | None:
        """运行 metric.json 指定的 eval 脚本，解析其 stdout 的 primary 分数。

        PREPARE 基线不再走冻结评估器 + ``--request/--output``；Agent 直接写
        ``metric.json``（如 ``{"eval_script": "evaluator/evaluate.py"}``），eval
        脚本以 workspace 为 cwd 读取 labels.csv 与 predictions.csv，打印一行
        ``{"primary": <float>}``。
        """
        spec_path = self.workdir / "metric.json"
        if not spec_path.is_file():
            return None
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            eval_script = spec["eval_script"]
        except (OSError, ValueError, KeyError):
            return None
        result = await self._execution.run(
            self._context,
            argv=["python", eval_script],
            timeout_s=self._timeout_s,
            workdir=str(self.workdir),
        )
        if not result.ok:
            return None
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        try:
            return float(json.loads(lines[-1])["primary"])
        except (ValueError, KeyError, TypeError):
            return None

    async def _failure(
        self,
        plan_id: str,
        kind: Literal[
            "scoring_failed",
            "evaluator_infrastructure_failed",
            "output_failed",
            "execution_failed",
            "manifest_invalid",
        ],
        error: str,
        *,
        predictions_ref: ArtifactRef | None = None,
    ) -> PlanTurnResult:
        cleaned = redact(" ".join(error.split()))[:1000]
        evidence_ref = await self._store.put_text(
            json.dumps(
                {
                    "plan": plan_id,
                    "kind": kind,
                    "error": cleaned,
                    "predictions_ref": predictions_ref,
                },
                ensure_ascii=False,
            )
        )
        return PlanTurnResult(
            kind=kind,
            predictions_ref=predictions_ref,
            evidence_ref=evidence_ref,
            error=cleaned,
        )

    async def _load_bundle(self, evaluator_ref: ArtifactRef) -> DataScriptBundle | None:
        """从 artifact 引用加载冻结评估 bundle；无效时返回 None。"""
        try:
            text = await self._store.get_text(evaluator_ref)
        except Exception:
            # artifact 缺失或读取失败 → 无法打分
            return None
        try:
            return DataScriptBundle.model_validate_json(text)
        except ValueError:
            return None

    async def _store_report(self, manifest: ExperimentManifest) -> ArtifactRef | None:
        """存储声明且存在的 report 输出；缺省时返回 None。"""
        report = manifest.outputs.get("report")
        if report is None:
            return None
        report_path = self.workdir / report
        if not report_path.is_file() or not report_path.stat().st_size:
            return None
        return await self._store.put_text(report_path.read_text(encoding="utf-8"))

"""Manifest execution, trusted scoring, and patience for one autonomous Plan.

复用既有能力：manifest 命令经 ``ExecutionRuntime`` 的 bounded argv 执行，
证据/预测经 ``LocalArtifactStore`` 存取，打分经 ``TrustedEvaluator`` 与冻结
``DataScriptBundle``，可信修订经 ``LocalGitWorkspace`` 的 review/commit 提交。
计划级状态合同来自 ``plans.py``（Task 1）。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    model_validator,
)

from athena.core.agent.types import RunStatus
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.core.workspace import GitWorkBranch, GitWorkspace
from athena.execution.runtime import (
    CommandRequest,
    ExecutionContext,
    ExecutionRuntime,
)
from athena.research.contracts import CandidateEvaluation, EvaluatorDescriptor
from athena.research.evaluation import TrustedEvaluator
from athena.research.evaluation.spec import load_evaluator_spec
from athena.research.exploration_files import (
    append_experiment_log,
    read_exploration_note,
)
from athena.research.output_freshness import (
    OutputFreshnessError,
    archive_output_roots,
    assert_output_roots,
)
from athena.research.prediction_contract import (
    PredictionInputError,
    assert_predictions_cover,
)
from athena.research.script_runner import load_directory, pack_directory
from athena.research.supervisor.events import bounded_redacted_tail
from athena.research.supervisor.manifest import (
    ExperimentManifest,
    read_experiment_manifest,
)
from athena.research.supervisor.plans import (
    DEFAULT_EXPERIMENT_TIMEOUT_S,
    PlanBest,
    PlanDecision,
    PlanFailure,
    PlanInput,
    PlanState,
)

Direction = Literal["maximize", "minimize"]

# 只有改动真正的实现源文件才算“实验”；只改 manifest/输出/文档会被拒绝。
_SEMANTIC_SOURCE_SUFFIXES = (".py", ".ipynb", ".sh", ".R", ".jl")
_NON_IMPLEMENTATION_MARKERS = (
    "predictions/",
    "report",
    "experiment.json",
    ".md",
)


def _diff_intervention_failure(paths: tuple[str, ...]) -> PlanFailure | None:
    """Return a rejection reason when a SEARCH diff does not implement source.

    This is a deterministic first line of defense for attribution: it does not
    prove the diff implements the exact intervention, but it rejects the obvious
    non-experiments (manifest-only, output-only, doc-only) that currently pass
    the “non-empty diff” gate.

    An unchanged entrypoint can import changed helpers or load them dynamically;
    manifest argv alone cannot establish whether a source change is executed.
    """
    if not paths:
        return PlanFailure(
            kind="no_change",
            detail=(
                "this candidate changed no file since the latest committed revision. "
                "Athena commits each successfully scored revision automatically. "
                "Implement a new source change, or submit an existing trusted best."
            ),
        )
    semantic = [
        path
        for path in paths
        if path.endswith(_SEMANTIC_SOURCE_SUFFIXES)
        and not any(marker in path for marker in _NON_IMPLEMENTATION_MARKERS)
    ]
    if not semantic:
        return PlanFailure(
            kind="diff_rejected",
            detail=(
                "this candidate changed no implementation source file since the "
                "latest committed revision; only manifest/output/documentation "
                "changed. Athena already commits each successfully scored revision. "
                "The comparison is against that latest commit, not always the "
                "original parent. Make a new source change, or submit the saved "
                "trusted best if no further change is intended."
            ),
        )
    return None


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


@dataclass(frozen=True, slots=True)
class SettlementDecision:
    """一次 turn 的终态动作：settle / wait / continue 及其依据。"""

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
        "execution_infrastructure_failed",
        "manifest_invalid",
        # SEARCH 候选一个文件都没改：它重跑的是父实验，测不了任何东西。
        "no_change",
        # SEARCH 候选只改了 manifest/输出/文档，没有实现假设中的源码改动。
        "diff_rejected",
    ]
    metric: float | None = None
    commit: CommitHash | None = None
    next_state: PlanState | None = None
    predictions_ref: ArtifactRef | None = None
    metrics_ref: ArtifactRef | None = None
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


async def load_best(best_ref: ArtifactRef, store: ArtifactStore) -> PlanBest:
    """从 artifact 引用加载不可变 PlanBest 记录。"""
    return PlanBest.model_validate_json(await store.get_text(best_ref))


async def apply_trusted_score(
    state: PlanState,
    best: PlanBest,
    *,
    store: ArtifactStore,
    direction: Direction = "maximize",
) -> PlanState:
    """应用一次可信分数：更新不可变 best 并调整 stale_rounds。

    优于历史 best → 写新 PlanBest、best_ref 指向它、stale_rounds 归零；
    未显著改进 → stale_rounds 加一且保留历史 best。返回 ``state`` 的副本。
    """
    current = await load_best(state.best_ref, store) if state.best_ref else None
    improved = current is None or (
        best.metric > current.metric
        if direction == "maximize"
        else best.metric < current.metric
    )
    if improved:
        best_ref = await store.put_text(best.model_dump_json())
        return state.model_copy(update={"best_ref": best_ref, "stale_rounds": 0})
    return state.model_copy(update={"stale_rounds": state.stale_rounds + 1})


def decide_settlement(
    state: PlanState,
    decision: PlanDecision,
    *,
    report_ref: ArtifactRef | None = None,
) -> SettlementDecision:
    """根据 Plan 预算与 Agent 决策返回 turn 的终态动作。

    submit/abandon 一律 settle（无 best 时按 LOSS settle）；continue 下 patience
    用尽或 turn 用尽且有 best → settle best；turn 用尽且无 best → wait。
    """
    patience_exhausted = (
        state.patience is not None
        and state.patience > 0
        and state.stale_rounds >= state.patience
    )
    turns_exhausted = (
        state.turn_limit is not None and state.turns_used >= state.turn_limit
    )
    if decision.decision in ("submit", "abandon"):
        settlement = SettlementDecision(
            action="settle", best_ref=state.best_ref, reason=decision.decision
        )
    elif patience_exhausted:
        settlement = SettlementDecision(
            action="settle", best_ref=state.best_ref, reason="patience exhausted"
        )
    elif turns_exhausted:
        if state.best_ref is not None:
            settlement = SettlementDecision(
                action="settle",
                best_ref=state.best_ref,
                reason="turn budget exhausted",
            )
        else:
            settlement = SettlementDecision(
                action="wait", reason="turn budget exhausted without a trusted best"
            )
    else:
        settlement = SettlementDecision(action="continue")
    # 只有 PREPARE 强制要求 report；SEARCH 的 report 输出是可选的，缺失不应卡住 settle。
    if settlement.action == "settle" and state.kind == "PREPARE" and report_ref is None:
        return SettlementDecision(
            action="wait", reason="report required before settlement"
        )
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
        timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S,
        placement: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._execution = execution
        self._store = store
        self._evaluator = evaluator
        self._workspace = workspace
        self._branch = branch
        self._context = context
        self._timeout_s = timeout_s
        self._placement = placement

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
            # Trigger: the submitted manifest is missing or fails schema validation.
            return await self._failure(
                plan_id,
                PlanFailure(
                    kind="manifest_invalid",
                    detail=" ".join(str(exc).split())[:1000],
                ),
            )

        failure = await self._execute_manifest(plan_id, state, manifest, emit)
        if failure is not None:
            return await self._failure(plan_id, failure)

        predictions_root = manifest.outputs["predictions"]
        predictions_dir = self.workdir / predictions_root
        try:
            predictions_ref = await pack_directory(self._store, predictions_dir)
            predictions = await load_directory(self._store, predictions_ref)
        except Exception as exc:  # noqa: BLE001 - artifact packing is infrastructure
            return await self._failure(
                plan_id,
                PlanFailure(
                    kind="execution_infrastructure_failed",
                    detail=f"stage=pack_predictions; {type(exc).__name__}: {exc}",
                ),
            )
        report_ref = await self._store_report(manifest)
        if state.kind == "PREPARE" and report_ref is None:
            return await self._failure(
                plan_id,
                PlanFailure(
                    kind="output_failed",
                    detail="PREPARE requires a declared, non-empty report output",
                ),
                predictions_ref=predictions_ref,
            )

        failure = await self._search_diff_failure(state)
        if failure is not None:
            return await self._failure(
                plan_id, failure, predictions_ref=predictions_ref
            )

        score = await self._score_candidate(
            plan_id, plan_input, predictions, predictions_root
        )
        if isinstance(score, PlanFailure):
            return await self._failure(plan_id, score, predictions_ref=predictions_ref)
        evaluation = score
        metric = evaluation.test_score
        metrics_ref = evaluation.metrics_ref

        append_experiment_log(
            self.workdir,
            f"## {plan_id}: scored\n\n- Metric: `{metric}`\n"
            f"- Metrics table: `{metrics_ref or 'none'}`\n"
            f"- Predictions: `{predictions_ref}`\n"
            f"- Report: `{report_ref or 'none'}`",
        )
        exploration_ref = await self._store_exploration()

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
                    "metrics_ref": metrics_ref,
                    "report_ref": report_ref,
                    "exploration_ref": exploration_ref,
                    "outputs": manifest.outputs,
                    **(
                        {"placement": self._placement()}
                        if self._placement is not None
                        else {}
                    ),
                },
                ensure_ascii=False,
            )
        )
        updated = state
        if state.kind == "SEARCH":
            updated = await apply_trusted_score(
                state,
                PlanBest(
                    metric=metric,
                    commit=commit,
                    evidence_ref=evidence_ref,
                    std_error=evaluation.test_se,
                    n=evaluation.test_n,
                ),
                store=self._store,
                direction=plan_input.direction,
            )
        return PlanTurnResult(
            kind="scored",
            metric=metric,
            commit=commit,
            next_state=updated,
            predictions_ref=predictions_ref,
            metrics_ref=metrics_ref,
            evidence_ref=evidence_ref,
            report_ref=report_ref,
        )

    async def _execute_manifest(
        self,
        plan_id: str,
        state: PlanState,
        manifest: ExperimentManifest,
        emit: EmitEvent | None,
    ) -> PlanFailure | None:
        """Archive old outputs, run commands, collect remote files, and check freshness."""
        version = f"{plan_id}-{state.turns_used}"
        try:
            archive_output_roots(self.workdir, manifest.outputs, version=version)
        except Exception as exc:  # noqa: BLE001 - archive failure is infrastructure
            return PlanFailure(
                kind="execution_infrastructure_failed",
                detail=f"stage=archive_outputs; {type(exc).__name__}: {exc}",
            )

        for index, argv in enumerate(manifest.commands, start=1):
            try:
                result = await self._execution.run(
                    self._context,
                    CommandRequest(
                        command=argv,
                        timeout_s=self._timeout_s,
                        workdir=str(self.workdir),
                        emit=emit,
                        evaluation_split="search",
                    ),
                )
            except (FileNotFoundError, ValueError) as exc:
                # Trigger: a command argv is missing/invalid or contains an unsafe workspace path.
                return PlanFailure(
                    kind="execution_failed",
                    detail=f"stage=command[{index}] argv={argv!r}; {type(exc).__name__}: {exc}",
                )
            except Exception as exc:  # noqa: BLE001 - backend/process infrastructure
                return PlanFailure(
                    kind="execution_infrastructure_failed",
                    detail=f"stage=command[{index}] argv={argv!r}; {type(exc).__name__}: {exc}",
                )
            if not result.ok:
                phase = (
                    "timeout"
                    if result.error == "timeout"
                    else f"exit={result.exit_code}"
                )
                output = "\n".join(
                    part
                    for part in (result.stdout, result.stderr, result.error or "")
                    if part
                )
                tail = (
                    bounded_redacted_tail(output, 550)
                    or "(no stdout/stderr was captured)"
                )
                error = (
                    f"stage=command[{index}] {phase}; argv={str(argv)[:150]}; "
                    f"output_tail={tail}"
                )
                if "ModuleNotFoundError" in output:
                    error += (
                        ' Run "uv sync --project $ATHENA_ENV_ROOT" to install the '
                        "declared dependencies into the environment venv, then retry."
                    )
                kind = (
                    "execution_infrastructure_failed"
                    if result.exit_code < 0 and result.error not in (None, "timeout")
                    else "execution_failed"
                )
                return PlanFailure(kind=kind, detail=error)

        try:
            await self._execution.collect_outputs(tuple(manifest.outputs.values()))
        except Exception as exc:  # noqa: BLE001 - remote collection/backend fault
            return PlanFailure(
                kind="execution_infrastructure_failed",
                detail=f"stage=collect_outputs; {type(exc).__name__}: {exc}",
            )
        required = {"predictions"}
        if state.kind == "PREPARE":
            required.add("report")
        try:
            assert_output_roots(self.workdir, manifest.outputs, required=required)
        except OutputFreshnessError as exc:
            # 命令未产生声明输出 → 同 Plan 修正后重试。
            return PlanFailure(kind="output_failed", detail=str(exc))
        return None

    async def _search_diff_failure(self, state: PlanState) -> PlanFailure | None:
        """Reject SEARCH turns that did not make an attributable source change."""
        if state.kind != "SEARCH":
            return None
        diff = await self._workspace.diff(self._branch)
        return _diff_intervention_failure(diff.paths)

    async def _score_candidate(
        self,
        plan_id: str,
        plan_input: PlanInput,
        predictions: dict[str, bytes],
        predictions_root: str,
    ) -> CandidateEvaluation | PlanFailure:
        """Score collected predictions or map the evaluator failure to its domain kind."""
        evaluator_dir = await self._load_evaluator_dir(plan_input.evaluator_ref)
        if evaluator_dir is None:
            return PlanFailure(
                kind="evaluator_infrastructure_failed",
                detail="evaluator descriptor is invalid or its directory is missing",
            )
        try:
            spec = (
                load_evaluator_spec(evaluator_dir)
                if (evaluator_dir / "metric.json").is_file()
                else None
            )
        except (OSError, ValueError, TypeError) as exc:
            # Trigger: the frozen evaluator descriptor or metric spec cannot be loaded or validated.
            return PlanFailure(
                kind="evaluator_infrastructure_failed",
                detail=f"frozen evaluator contract is invalid: {exc}",
            )
        if self._context.predict_features is not None:
            prediction_file = spec.prediction_file if spec is not None else None
            # Match TrustedEvaluator's existing single-CSV legacy filename alias.
            csv_files = [
                name for name in predictions if Path(name).suffix.lower() == ".csv"
            ]
            if prediction_file not in predictions and len(csv_files) == 1:
                prediction_file = csv_files[0]
            try:
                assert_predictions_cover(
                    self.workdir / predictions_root,
                    self._context.predict_features,
                    prediction_file=prediction_file,
                    prediction_id_column=spec.prediction_id_column if spec else None,
                    prediction_format=spec.prediction_format if spec else "tabular_csv",
                )
            except PredictionInputError as exc:
                # Trigger: declared prediction inputs do not satisfy frozen feature/ID coverage.
                return PlanFailure(
                    kind="execution_infrastructure_failed", detail=str(exc)
                )
            except (ValueError, UnicodeError) as exc:
                # Trigger: prediction output files are malformed or violate the declared format contract.
                return PlanFailure(kind="scoring_failed", detail=str(exc))
            except OSError as exc:
                # Trigger: prediction files are inaccessible despite a valid output contract.
                return PlanFailure(
                    kind="execution_infrastructure_failed", detail=str(exc)
                )
        try:
            evaluation = await self._evaluator.score(
                evaluator_dir=evaluator_dir,
                predictions=predictions,
                candidate_id=plan_id,
                direction=plan_input.direction,
                predictions_root=predictions_root,
            )
        except ValueError as exc:
            # 候选输出不符合评估契约 → 同 Plan 修正后重试
            return PlanFailure(kind="scoring_failed", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - map evaluator infrastructure failure
            # 评估基础设施异常 → 保留独立错误映射供上层终止
            return PlanFailure(kind="evaluator_infrastructure_failed", detail=str(exc))
        return evaluation

    async def _failure(
        self,
        plan_id: str,
        failure: PlanFailure,
        *,
        predictions_ref: ArtifactRef | None = None,
    ) -> PlanTurnResult:
        """Persist a normalized Plan failure and return its non-scored result."""
        cleaned = bounded_redacted_tail(failure.detail, 1000)
        append_experiment_log(
            self.workdir,
            f"## {plan_id}: {failure.kind}\n\n- Error: {cleaned}",
        )
        exploration_ref = await self._store_exploration()
        evidence_ref = await self._store.put_text(
            json.dumps(
                {
                    "plan": plan_id,
                    "kind": failure.kind,
                    "error": cleaned,
                    "predictions_ref": predictions_ref,
                    "exploration_ref": exploration_ref,
                    **(
                        {"placement": self._placement()}
                        if self._placement is not None
                        else {}
                    ),
                },
                ensure_ascii=False,
            )
        )
        return PlanTurnResult(
            kind=failure.kind,
            predictions_ref=predictions_ref,
            evidence_ref=evidence_ref,
            error=cleaned,
        )

    async def _load_evaluator_dir(self, evaluator_ref: ArtifactRef) -> Path | None:
        """Load the README-only evaluator directory from its descriptor."""
        try:
            text = await self._store.get_text(evaluator_ref)
        except (OSError, ValueError):
            # Trigger: the evaluator descriptor artifact cannot be read from the store.
            return None
        try:
            descriptor = EvaluatorDescriptor.model_validate_json(text)
        except ValueError:
            # Trigger: the stored evaluator descriptor is malformed or violates its schema.
            return None
        path = Path(descriptor.dir_path)
        if not path.is_dir() or not (path / "README.md").is_file():
            return None
        return path

    async def _store_report(self, manifest: ExperimentManifest) -> ArtifactRef | None:
        """存储声明且存在的 report 输出；缺省时返回 None。"""
        report = manifest.outputs.get("report")
        if report is None:
            return None
        report_path = self.workdir / report
        if not report_path.is_file() or not report_path.stat().st_size:
            return None
        return await self._store.put_text(report_path.read_text(encoding="utf-8"))

    async def _store_exploration(self) -> ArtifactRef | None:
        """Store the optional Agent-authored exploration note for this turn."""
        content = read_exploration_note(self.workdir)
        return await self._store.put_text(content) if content is not None else None

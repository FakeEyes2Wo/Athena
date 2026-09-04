"""Manifest execution, trusted scoring, and patience for one autonomous Plan.

复用既有能力：manifest 命令经 ``ExecutionRuntime`` 的 bounded argv 执行，
证据/预测经 ``LocalArtifactStore`` 存取，打分经 ``TrustedEvaluator`` 与冻结
``DataScriptBundle``，可信修订经 ``LocalGitWorkspace`` 的 review/commit 提交。
计划级状态合同来自 ``plans.py``（Task 1）。
"""

import json
from collections.abc import Callable
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
from athena.research.exploration_files import (
    append_experiment_log,
    read_exploration_note,
)
from athena.research.predictions_cover import (
    PredictionsCoverageError,
    assert_predictions_cover,
)
from athena.research.output_freshness import (
    OutputFreshnessError,
    archive_output_roots,
    assert_output_roots,
)
from athena.research.script_runner import load_directory, pack_directory
from athena.research.supervisor.events import redact
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


def _diff_implements_intervention(paths: tuple[str, ...]) -> str | None:
    """Return a rejection reason when a SEARCH diff does not implement source.

    This is a deterministic first line of defense for attribution: it does not
    prove the diff implements the exact intervention, but it rejects the obvious
    non-experiments (manifest-only, output-only, doc-only) that currently pass
    the “non-empty diff” gate.
    """
    if not paths:
        return (
            "this candidate changed no file, so it re-ran the parent unchanged and "
            "cannot test anything. Implement the intervention in source code."
        )
    semantic = [
        path
        for path in paths
        if path.endswith(_SEMANTIC_SOURCE_SUFFIXES)
        and not any(marker in path for marker in _NON_IMPLEMENTATION_MARKERS)
    ]
    if not semantic:
        return (
            "this candidate changed no implementation source file; only "
            "manifest/output/documentation changed. Implement the intervention "
            "in a source file (e.g. model.py, features.py, train.py)."
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
    metric: float,
    commit: CommitHash,
    *,
    store: ArtifactStore,
    evidence_ref: ArtifactRef,
    direction: Direction = "maximize",
    std_error: float | None = None,
    n: int | None = None,
) -> PlanState:
    """应用一次可信分数：更新不可变 best 并调整 stale_rounds。

    优于历史 best → 写新 PlanBest、best_ref 指向它、stale_rounds 归零；
    未显著改进 → stale_rounds 加一且保留历史 best。返回 ``state`` 的副本。
    """
    current = await load_best(state.best_ref, store) if state.best_ref else None
    improved = current is None or (
        metric > current.metric if direction == "maximize" else metric < current.metric
    )
    if improved:
        best = PlanBest(
            metric=metric,
            commit=commit,
            evidence_ref=evidence_ref,
            std_error=std_error,
            n=n,
        )
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
        # 打分之前先确认预测答的是要被打分的那些行。VALIDATE 一直有这道检查，
        # PREPARE/SEARCH 没有——于是 2026-09-02 的基线把行号当行 id 写了出去，
        # 169725 行标签里只有 36674 行 join 上，而且每一行配到的都是别的窗口的
        # 预测。Agent 自测 PR-AUC 0.7982，可信评估器返回 0.016331，框架把它当成
        # 了整轮的参考指标。坏的参考指标不会让运行失败，它只是悄悄改变了标尺。
        if self._context.predict_features is not None:
            try:
                assert_predictions_cover(
                    predictions_dir, Path(self._context.predict_features)
                )
            except PredictionsCoverageError as exc:
                return await self._failure(
                    plan_id,
                    PlanFailure(kind="output_failed", detail=str(exc)),
                )
        predictions_ref = await pack_directory(self._store, predictions_dir)
        predictions = await load_directory(self._store, predictions_ref)
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
                metric,
                commit,
                store=self._store,
                evidence_ref=evidence_ref,
                direction=plan_input.direction,
                std_error=evaluation.test_se,
                n=evaluation.test_n,
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
        except Exception as exc:  # noqa: BLE001 - archive failure is an output failure
            return PlanFailure(
                kind="output_failed", detail=f"failed to archive old outputs: {exc}"
            )

        for argv in manifest.commands:
            result = await self._execution.run(
                self._context,
                CommandRequest(
                    argv=argv,
                    timeout_s=self._timeout_s,
                    workdir=str(self.workdir),
                    emit=emit,
                    evaluation_split="search",
                ),
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
                return PlanFailure(kind="execution_failed", detail=error)

        await self._execution.collect_outputs(tuple(manifest.outputs.values()))
        required = {"predictions"}
        if state.kind == "PREPARE":
            required.add("report")
        try:
            assert_output_roots(self.workdir, manifest.outputs, required=required)
        except OutputFreshnessError as exc:
            # 命令未产生声明输出 → 同 Plan 修正后重试
            return PlanFailure(kind="output_failed", detail=str(exc))
        return None

    async def _search_diff_failure(self, state: PlanState) -> PlanFailure | None:
        """Reject SEARCH turns that did not make an attributable source change."""
        if state.kind != "SEARCH":
            return None
        diff = await self._workspace.diff(self._branch)
        rejected = _diff_implements_intervention(diff.paths)
        if rejected is None:
            return None
        kind = "no_change" if not diff.paths else "diff_rejected"
        if kind == "no_change":
            rejected = (
                "this candidate changed no file, so it re-ran the parent unchanged and "
                "cannot test anything. Implement the intervention described in the "
                "hypothesis — edit the solution sources, then rerun and submit."
            )
        return PlanFailure(kind=kind, detail=rejected)

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
                kind="scoring_failed",
                detail="evaluator descriptor is invalid or its directory is missing",
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
        cleaned = redact(" ".join(failure.detail.split()))[:1000]
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
            return None
        try:
            descriptor = EvaluatorDescriptor.model_validate_json(text)
        except ValueError:
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

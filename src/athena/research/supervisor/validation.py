"""Independent frozen-SOTA validation orchestration."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.task_agents import (
    VALIDATE_AGENT_ID,
    VALIDATE_AGENT_TYPE,
    ValidationRepair,
)
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.core.workspace import GitDiff, GitWorkBranch, GitWorkspace
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.contracts import DataScriptBundle, ValidationResult
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import load_directory, pack_directory
from athena.research.supervisor.events import redact
from athena.research.supervisor.experiment import (
    load_agent_result,
    read_experiment_manifest,
)
from athena.research.validation import ValidationService

CheckpointValidation = Callable[[ArtifactRef], Awaitable[None]]
_MAX_REVIEW_DIFF_CHARS = 12_000
# 校验修复循环的迭代上限，防止 preflight/review/工作区变化互相拉锯造成无限烧 token。
_MAX_VALIDATION_REPAIR_ATTEMPTS = 16


class ValidationInput(BaseModel):
    """Immutable inputs identifying one logical validation attempt."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sota_commit: CommitHash
    reference_metric: float = Field(allow_inf_nan=False)
    direction: Literal["maximize", "minimize"]
    final_evaluator_ref: ArtifactRef
    validation_key: str = Field(min_length=1)
    # 只读 SOTA 上下文（假设/干预/预期效果/metric/commit），供 ValidateAgent
    # 审阅 diff 时知晓被验证对象；绝不进入 validation_key（身份仍由
    # commit+metric+direction+evaluator 决定）。
    sota_context: dict[str, Any] | None = None


class ValidationDiffReview(BaseModel):
    """Independent decision on a proposed runtime-only repair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    accepted: bool
    reason: str = Field(min_length=1)


def validation_key(
    sota_commit: CommitHash,
    reference_metric: float,
    direction: Literal["maximize", "minimize"],
    final_evaluator_ref: ArtifactRef,
) -> str:
    """Return the stable identity of exactly one frozen validation input."""

    payload = {
        "direction": direction,
        "final_evaluator_ref": final_evaluator_ref,
        "reference_metric": reference_metric,
        "sota_commit": sota_commit,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def recovery_action(
    result_ref: ArtifactRef | None,
    store: ArtifactStore,
    input: ValidationInput,
) -> Literal["run", "score", "commit"]:
    """Choose the next action from durable validation output."""

    if result_ref is None:
        return "run"
    try:
        result = ValidationResult.model_validate_json(await store.get_text(result_ref))
    except (OSError, ValueError):
        return "run"
    if (
        result.result_id != input.validation_key
        or result.sota_commit != input.sota_commit
    ):
        return "run"
    if result.predictions_ref is None or result.evidence_ref is None:
        if result.final_test_score is not None:
            raise RuntimeError("scored validation result has no reviewed diff evidence")
        return "run"
    try:
        evidence = json.loads(await store.get_text(result.evidence_ref))
        GitDiff(
            ref=evidence["reviewed_diff_ref"],
            paths=tuple(evidence["reviewed_diff_paths"]),
        )
    except (OSError, KeyError, TypeError, ValueError):
        if result.final_test_score is not None:
            raise RuntimeError(
                "scored validation result has invalid reviewed diff evidence"
            )
        return "run"
    if result.final_test_score is None:
        return "score"
    return "commit"


_SEMANTIC_FILENAMES = frozenset(
    {
        "features.py",
        "model.py",
        "preprocess.py",
        "preprocessing.py",
        "train.py",
        "training.py",
    }
)
_LEAKAGE_MARKERS = (
    "final_label",
    "final-label",
    "final target",
    "final_target",
    "y_final",
)
_SEMANTIC_MARKERS = (
    "hidden_size",
    "learning_rate",
    "layers =",
    "epochs =",
    "features.append",
    "normalize(",
)


async def _reviewed_diff_text(diff: GitDiff, store: ArtifactStore) -> str | None:
    """Load, redact, and bound the exact diff artifact approved for commit."""

    raw = await store.get_bytes(diff.ref)
    if b"\x00" in raw or b"GIT binary patch" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = redact(text)
    if len(text) > _MAX_REVIEW_DIFF_CHARS:
        text = text[:_MAX_REVIEW_DIFF_CHARS] + "\n[DIFF TRUNCATED]"
    return text


async def review_validation_diff(
    *,
    workspace: GitWorkBranch,
    diff: GitDiff,
    explanation: str,
    independent_review: Callable[[str], Awaitable[ValidationDiffReview]],
    store: ArtifactStore,
) -> ValidationDiffReview:
    """Accept only explained runtime repairs approved by an independent reviewer."""

    if not explanation.strip():
        return ValidationDiffReview(
            accepted=False,
            reason="validation repair requires an agent explanation",
        )
    semantic_paths = [
        path for path in diff.paths if Path(path).name.lower() in _SEMANTIC_FILENAMES
    ]
    if semantic_paths:
        return ValidationDiffReview(
            accepted=False,
            reason="validation cannot change model or training semantics",
        )
    del workspace
    changed_text = await _reviewed_diff_text(diff, store)
    if changed_text is None:
        return ValidationDiffReview(
            accepted=False,
            reason="validation diff is binary or cannot be reviewed safely",
        )
    lowered = changed_text.lower()
    if any(marker in lowered for marker in _LEAKAGE_MARKERS):
        return ValidationDiffReview(
            accepted=False,
            reason="validation repair may not access final labels",
        )
    if any(marker in lowered for marker in _SEMANTIC_MARKERS):
        return ValidationDiffReview(
            accepted=False,
            reason="validation cannot change model or training semantics",
        )
    prompt = json.dumps(
        {
            "policy": "Accept runtime-only repairs; reject semantic tuning or label access.",
            "paths": list(diff.paths),
            "explanation": explanation.strip(),
            "changed_text": changed_text,
        },
        sort_keys=True,
    )
    return await independent_review(prompt)


async def _load_result(
    result_ref: ArtifactRef | None, store: ArtifactStore
) -> ValidationResult | None:
    if result_ref is None:
        return None
    try:
        return ValidationResult.model_validate_json(await store.get_text(result_ref))
    except (OSError, ValueError):
        return None


async def _save_result(
    result: ValidationResult,
    store: ArtifactStore,
    checkpoint: CheckpointValidation,
) -> ArtifactRef:
    result_ref = await store.put_text(result.model_dump_json())
    await checkpoint(result_ref)
    return result_ref


async def _decode_repair(
    agents: AgentRuntime,
    store: ArtifactStore,
    *,
    input: ValidationInput,
    feedback: str | None = None,
) -> ValidationRepair:
    task = {
        "content": feedback
        or "Run frozen-SOTA validation and repair runtime-only failures."
    }
    if feedback is None and input.sota_context:
        task["content"] = (
            "Validating the trusted SOTA hypothesis:\n"
            f"{json.dumps(input.sota_context, ensure_ascii=False)}\n\n"
            + task["content"]
        )
    if feedback is None:
        _agent_id, run_id = await agents.create_root(
            VALIDATE_AGENT_TYPE,
            task,
            name=VALIDATE_AGENT_ID,
            agent_id=VALIDATE_AGENT_ID,
        )
    else:
        run_id = await agents.followup(VALIDATE_AGENT_ID, task)
    summary = await agents.wait_run(run_id)
    repair = await load_agent_result(summary, store, ValidationRepair)
    if repair is None:
        raise RuntimeError(summary.error or "validate Agent failed")
    return repair


async def _execute_predictions(
    *,
    execution: ExecutionRuntime,
    git: GitWorkspace,
    workspace: GitWorkBranch,
    store: ArtifactStore,
    publish: EmitEvent | None,
) -> tuple[ArtifactRef, str]:
    workdir = Path(workspace.path)
    manifest = read_experiment_manifest(workdir)
    context = ExecutionContext(
        project_root=workdir,
        workspace_root=workdir,
        environment_root=getattr(execution, "environment_root", workdir),
    )
    try:
        for argv in manifest.commands:
            result = await execution.run(
                context,
                argv=argv,
                workdir=workdir,
                emit=publish,
            )
            if not result.ok:
                raise RuntimeError(result.stderr or "validation command failed")
        rel_path = manifest.outputs["predictions"]
        predictions_dir = workdir / rel_path
        if not predictions_dir.is_dir() or not any(predictions_dir.iterdir()):
            raise ValueError("validation predictions output is missing")
        ref = await pack_directory(store, predictions_dir)
        return ref, rel_path
    finally:
        await git.restore_paths(workspace, tuple(manifest.outputs.values()))


async def _deterministic_preflight(
    *,
    workspace: GitWorkBranch,
    diff: GitDiff,
    explanation: str,
    store: ArtifactStore,
) -> ValidationDiffReview:
    """Run the deterministic policy before executing the proposed repair."""

    async def accept(_prompt: str) -> ValidationDiffReview:
        return ValidationDiffReview(accepted=True, reason="deterministic preflight")

    return await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation=explanation,
        independent_review=accept,
        store=store,
    )


async def _score_result(
    *,
    input: ValidationInput,
    current: ValidationResult,
    evaluator: TrustedEvaluator,
    store: ArtifactStore,
) -> ValidationResult:
    if current.predictions_ref is None:
        raise ValueError("validation scoring requires predictions")
    bundle = DataScriptBundle.model_validate_json(
        await store.get_text(input.final_evaluator_ref)
    )
    predictions = await load_directory(store, current.predictions_ref)
    evaluation = await evaluator.score(
        eval_bundle=bundle,
        predictions=predictions,
        candidate_id=input.validation_key,
        direction=input.direction,
        predictions_root=current.predictions_path or "predictions",
    )
    review_evidence = {}
    if current.evidence_ref is not None:
        review_evidence = json.loads(await store.get_text(current.evidence_ref))
    evidence_ref = await store.put_text(
        json.dumps(
            {
                **review_evidence,
                "validation_key": input.validation_key,
                "sota_commit": input.sota_commit,
                "predictions_ref": current.predictions_ref,
                "final_test_score": evaluation.test_score,
            },
            sort_keys=True,
        )
    )
    update = {
        "status": "COMPLETED",
        "test_score": input.reference_metric,
        "final_test_score": evaluation.test_score,
        "evidence_ref": evidence_ref,
    }
    service_result = ValidationService().build_result(
        test_score=input.reference_metric,
        final_test_score=evaluation.test_score,
        direction=input.direction,
    )
    update.update(
        generalization_gap=service_result.generalization_gap,
        generalization_warning=service_result.generalization_warning,
    )
    return current.model_copy(update=update)


async def run_validation_plan(
    *,
    input: ValidationInput,
    agents: AgentRuntime,
    git: GitWorkspace,
    workspace: GitWorkBranch,
    execution: ExecutionRuntime,
    evaluator: TrustedEvaluator,
    store: ArtifactStore,
    independent_review: Callable[[str], Awaitable[ValidationDiffReview]],
    result_ref: ArtifactRef | None,
    checkpoint: CheckpointValidation,
    publish: EmitEvent | None = None,
) -> ValidationResult:
    """Run or recover one independent validation attempt under its stable key."""

    try:
        expected_key = validation_key(
            input.sota_commit,
            input.reference_metric,
            input.direction,
            input.final_evaluator_ref,
        )
        if input.validation_key != expected_key:
            raise ValueError("validation_key does not match frozen validation inputs")
        current = await _load_result(result_ref, store)
        action = await recovery_action(result_ref, store, input)
        if (
            action == "commit"
            and current is not None
            and current.validation_commit is not None
            and current.final_test_score is not None
            and current.evidence_ref is not None
        ):
            return current

        if action == "run":
            repair = await _decode_repair(agents, store, input=input)
            for _ in range(_MAX_VALIDATION_REPAIR_ATTEMPTS):
                diff = await git.diff(workspace)
                preflight = await _deterministic_preflight(
                    workspace=workspace,
                    diff=diff,
                    explanation=repair.explanation,
                    store=store,
                )
                if not preflight.accepted:
                    repair = await _decode_repair(
                        agents,
                        store,
                        input=input,
                        feedback=f"Validation policy rejected the repair: {preflight.reason}",
                    )
                    continue
                review = await review_validation_diff(
                    workspace=workspace,
                    diff=diff,
                    explanation=repair.explanation,
                    independent_review=independent_review,
                    store=store,
                )
                if not review.accepted:
                    repair = await _decode_repair(
                        agents,
                        store,
                        input=input,
                        feedback=f"Independent review rejected the repair: {review.reason}",
                    )
                    continue
                reviewed_diff = diff
                predictions_ref, predictions_path = await _execute_predictions(
                    execution=execution,
                    git=git,
                    workspace=workspace,
                    store=store,
                    publish=publish,
                )
                if await git.diff(workspace) != reviewed_diff:
                    repair = await _decode_repair(
                        agents,
                        store,
                        input=input,
                        feedback="Validation workspace changed after independent review",
                    )
                    continue
                break
            else:
                raise RuntimeError(
                    "validation repair budget exhausted after "
                    f"{_MAX_VALIDATION_REPAIR_ATTEMPTS} attempts"
                )
            review_evidence_ref = await store.put_text(
                json.dumps(
                    {
                        "reviewed_diff_ref": reviewed_diff.ref,
                        "reviewed_diff_paths": list(reviewed_diff.paths),
                    },
                    sort_keys=True,
                )
            )
            current = ValidationResult(
                result_id=input.validation_key,
                status="FAILED",
                test_score=input.reference_metric,
                sota_commit=input.sota_commit,
                predictions_ref=predictions_ref,
                predictions_path=predictions_path,
                evidence_ref=review_evidence_ref,
            )
            await _save_result(current, store, checkpoint)
            action = "score"

        if action == "score":
            if current is None:
                raise RuntimeError("validation recovery result is unavailable")
            current = await _score_result(
                input=input,
                current=current,
                evaluator=evaluator,
                store=store,
            )
            await _save_result(current, store, checkpoint)
            action = "commit"

        if action == "commit":
            if current is None:
                raise RuntimeError("validation recovery result is unavailable")
            reviewed_diff = None
            if current.evidence_ref is not None:
                try:
                    evidence = json.loads(await store.get_text(current.evidence_ref))
                    reviewed_diff = GitDiff(
                        ref=evidence["reviewed_diff_ref"],
                        paths=tuple(evidence["reviewed_diff_paths"]),
                    )
                except (KeyError, TypeError, ValueError):
                    reviewed_diff = None
            if reviewed_diff is None:
                raise RuntimeError("validation commit requires reviewed diff evidence")
            diff = await git.diff(workspace)
            if diff != reviewed_diff:
                raise RuntimeError(
                    "validation workspace changed after independent review"
                )
            commit = await git.commit(
                workspace,
                diff,
                f"validate frozen SOTA {input.sota_commit}",
            )
            current = current.model_copy(update={"validation_commit": commit})
            await _save_result(current, store, checkpoint)
            return current

        raise RuntimeError(f"unsupported validation recovery action: {action}")
    finally:
        # The validate Agent is a one-shot VALIDATE worker; release it after the
        # phase succeeds or fails so repeated validation runs do not accumulate.
        try:
            await agents.reap(VALIDATE_AGENT_ID)
        except Exception:  # noqa: BLE001,S110 - GC must never mask VALIDATE failure
            pass


__all__ = [
    "CheckpointValidation",
    "ValidationDiffReview",
    "ValidationInput",
    "recovery_action",
    "review_validation_diff",
    "run_validation_plan",
    "validation_key",
]

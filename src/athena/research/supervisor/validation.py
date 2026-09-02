"""Independent frozen-SOTA validation orchestration."""

import csv
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from athena.execution.runtime import (
    CommandRequest,
    ExecutionContext,
    ExecutionRuntime,
)
from athena.research.contracts import EvaluatorDescriptor, ValidationResult
from athena.research.evaluation import TrustedEvaluator
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
from athena.research.supervisor.experiment import (
    load_agent_result,
    read_experiment_manifest,
)
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S
from athena.research.validation import ValidationService

CheckpointValidation = Callable[[ArtifactRef], Awaitable[None]]
_MAX_REVIEW_DIFF_CHARS = 12_000
# 校验修复循环的迭代上限，防止 preflight/review/工作区变化互相拉锯造成无限烧 token。
_MAX_VALIDATION_REPAIR_ATTEMPTS = 16
# 回灌给 validate Agent 的失败正文上限。候选脚本的 traceback 可以有几十 KB，
# 而有用的部分（异常类型与最后几帧）在末尾，所以超长时保留尾部。
_MAX_FAILURE_FEEDBACK_CHARS = 4_000


class ValidationRunFailed(RuntimeError):
    """The frozen command ran and failed — a candidate defect, not a bug here.

    Subclasses ``RuntimeError`` because that is what this raised before the
    repair loop learned to feed failures back, and callers outside the loop
    still catch the broad type.
    """


class PredictionsRejected(ValueError):
    """The command exited 0 but its predictions cannot be scored.

    A missing/empty declared output, or rows that do not answer the file
    VALIDATE asked about. Same repairability as ``ValidationRunFailed``;
    subclasses ``ValueError`` for the same backward-compatibility reason.
    """


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


@dataclass
class ValidationDeps:
    """All injected collaborators owned by one validation phase."""

    agents: AgentRuntime
    git: GitWorkspace
    workspace: GitWorkBranch
    execution: ExecutionRuntime
    evaluator: TrustedEvaluator
    store: ArtifactStore
    independent_review: Callable[[str], Awaitable[ValidationDiffReview]]
    checkpoint: CheckpointValidation
    publish: EmitEvent | None = None


@dataclass
class ValidationOptions:
    """Per-attempt validation knobs that are not part of the logical input."""

    timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S
    predict_features: Path | None = None
    data_csv: Path | None = None


@dataclass(frozen=True)
class PredictionRun:
    """One re-run of the frozen experiment and its packed predictions."""

    predictions_ref: ArtifactRef
    predictions_path: str


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

_ValidationRejectionSignature = tuple[str, ArtifactRef, str]


def _record_validation_rejection(
    previous: _ValidationRejectionSignature | None,
    current: _ValidationRejectionSignature,
) -> _ValidationRejectionSignature:
    """Stop when an unchanged repair reaches the same rejection stage twice."""
    if previous is not None and previous[:2] == current[:2]:
        stage, diff_ref, reason = current
        raise RuntimeError(
            "validation repair made no progress: same diff and rejection repeated; "
            f"stage={stage}; diff_ref={diff_ref}; reason={reason}"
        )
    return current


_DIFF_HEADER = re.compile(rb"^diff --git a/(?P<a>.*?) b/(?P<b>.*?)$", re.MULTILINE)


def _under(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` or lives inside it (either slash)."""
    norm = path.replace("\\", "/").strip("/")
    base = root.replace("\\", "/").strip("/")
    return bool(base) and (norm == base or norm.startswith(base + "/"))


MANIFEST_NAME = "experiment.json"


def declared_output_paths(
    workspace: GitWorkBranch, changed: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """The manifest's declared outputs, or none if they cannot be trusted.

    The manifest read here is the one in the worktree, which the Agent can edit.
    That would otherwise be a way to hide a source change from review: declare
    ``solution`` an output and its diff disappears. So if this repair touched
    the manifest at all, nothing is excluded and the reviewer sees everything —
    hiding then costs the very edit that makes it visible.
    """
    if any(Path(path).name == MANIFEST_NAME for path in changed):
        return ()
    try:
        manifest = read_experiment_manifest(Path(workspace.path))
    except (OSError, ValueError):
        # A broken manifest is the Agent's to repair; the review still runs.
        return ()
    return tuple(manifest.outputs.values())


def drop_output_sections(raw: bytes, outputs: tuple[str, ...]) -> bytes:
    """Remove the declared outputs' file sections from a unified diff.

    The validate Agent is told to repair the frozen command, which means running
    it — and running it writes exactly the paths the manifest declares. Those
    land in the worktree diff, where they do two kinds of damage.

    The reviewer sees a rewritten predictions file and reads it as tampering
    with the scored artifact. On 2026-09-02 it rejected a correct repair for
    precisely that: "replacing the entire prediction file implies a change in
    the model's inference results". And a 169,965-row predictions.csv is far
    past ``_MAX_REVIEW_DIFF_CHARS``; ``predictions/`` sorts before ``solution/``,
    so truncation cut the source change out of the prompt entirely. The reviewer
    was rejecting a repair it could not see.

    Dropping them is safe *because* of the freshness guard: ``_execute_predictions``
    archives every declared output before the command runs, so whatever the Agent
    left there is moved aside and the scored artifact can only be the command's
    own. Outputs are not review material — the review judges the repair.
    """
    if not outputs:
        return raw
    matches = list(_DIFF_HEADER.finditer(raw))
    if not matches:
        return raw
    kept = [raw[: matches[0].start()]]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        path = match.group("b").decode("utf-8", "replace")
        if not any(_under(path, out) for out in outputs):
            kept.append(raw[match.start() : end])
    return b"".join(kept)


async def _reviewed_diff_text(
    diff: GitDiff,
    store: ArtifactStore,
    *,
    outputs: tuple[str, ...] = (),
) -> str | None:
    """Load, redact, and bound the repair being reviewed, minus its outputs.

    The binary and NUL checks run on what *remains*: a binary declared output
    (a pickled model beside the predictions) used to fail the whole review as
    unreviewable, even though the repair itself was plain text.
    """

    raw = drop_output_sections(await store.get_bytes(diff.ref), outputs)
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


_ARTIFACT_SUFFIXES = (
    ".md",
    ".csv",
    ".tsv",
    ".txt",
    ".log",
    ".json",
    ".parquet",
    ".png",
    ".jpg",
    ".svg",
    ".pdf",
    ".html",
)


def _changed_lines(raw: bytes, *, skip_artifacts: bool) -> str:
    """The diff's added and removed lines, for marker scanning.

    Two properties the whole-diff text does not have.

    **Context lines are excluded.** Git carries three lines of context around
    every hunk, so a marker sitting *near* an edit rejected the edit. A LightGBM
    solution has ``learning_rate`` in its parameter dict; a pure path repair three
    lines away was unfixable, because the marker was not in anything the Agent
    had written.

    **Artifacts are excluded** when ``skip_artifacts``. The semantic gate exists
    to stop the Agent retuning the model. A generated ``REPORT.md`` that
    *describes* the run, or a predictions CSV, is an output. On 2026-09-02 the
    validate Agent ran the frozen command from inside ``solution/``, so its
    report landed at ``solution/REPORT.md`` -- outside the declared outputs, so
    ``drop_output_sections`` left it in -- and that report says ``learning_rate:``
    twice. All 16 repair attempts were rejected with the same sentence, and none
    of them could have removed it.

    Leakage keeps scanning artifacts: a predictions file that suddenly carries a
    final-label column is worth stopping.
    """
    kept: list[str] = []
    matches = list(_DIFF_HEADER.finditer(raw))
    if not matches:
        return raw.decode("utf-8", "replace")
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        path = match.group("b").decode("utf-8", "replace")
        if skip_artifacts and path.lower().endswith(_ARTIFACT_SUFFIXES):
            continue
        section = raw[match.start() : end].decode("utf-8", "replace")
        kept.extend(
            line
            for line in section.splitlines()
            if (line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        )
    return "\n".join(kept)


def _marker_hit(text: str, markers: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    return next((marker for marker in markers if marker in lowered), None)


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
    outputs = declared_output_paths(workspace, diff.paths)
    source_paths = [
        path
        for path in diff.paths
        if not any(_under(path, out) for out in outputs)
    ]
    semantic_paths = [
        path for path in source_paths if Path(path).name.lower() in _SEMANTIC_FILENAMES
    ]
    if semantic_paths:
        return ValidationDiffReview(
            accepted=False,
            reason=(
                "validation cannot change model or training semantics: it edits "
                + ", ".join(sorted(semantic_paths))
            ),
        )
    changed_text = await _reviewed_diff_text(diff, store, outputs=outputs)
    if changed_text is None:
        return ValidationDiffReview(
            accepted=False,
            reason="validation diff is binary or cannot be reviewed safely",
        )
    scanned = drop_output_sections(await store.get_bytes(diff.ref), outputs)
    leak = _marker_hit(_changed_lines(scanned, skip_artifacts=False), _LEAKAGE_MARKERS)
    if leak is not None:
        return ValidationDiffReview(
            accepted=False,
            reason=(
                "validation repair may not access final labels: a changed line "
                f"contains {leak!r}"
            ),
        )
    semantic = _marker_hit(
        _changed_lines(scanned, skip_artifacts=True), _SEMANTIC_MARKERS
    )
    if semantic is not None:
        # 说清楚是哪个词命中的。只回一句“不能改模型语义”时，Agent 连续 16 次
        # 交出同一个修复——它没有任何线索知道要改什么（2026-09-02）。
        return ValidationDiffReview(
            accepted=False,
            reason=(
                "validation cannot change model or training semantics: a changed "
                f"line contains {semantic!r}. Remove that edit; VALIDATE re-runs "
                "the frozen command and must not retune it."
            ),
        )
    payload: dict[str, Any] = {
        "policy": "Accept runtime-only repairs; reject semantic tuning or label access.",
        "paths": source_paths,
        "explanation": explanation.strip(),
        "changed_text": changed_text,
    }
    if len(source_paths) != len(diff.paths):
        # 不说明的话，评审会以为候选偷偷不改代码就改了产物。
        payload["excluded_outputs"] = (
            "Declared experiment outputs are excluded from this diff. The Agent "
            "runs the command to test its repair, which rewrites them; they are "
            "archived and regenerated by the frozen command before scoring, so "
            "their contents are not evidence about the repair. Judge the source "
            "change only."
        )
    return await independent_review(json.dumps(payload, sort_keys=True))


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
    timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S,
    predict_features: Path | None = None,
    data_csv: Path | None = None,
    version: str | None = None,
) -> PredictionRun:
    """Re-run the frozen SOTA experiment and pack its predictions.

    ``timeout_s`` must be passed explicitly. It used to be omitted, so this call
    silently took ``ExecutionRuntime.run``'s 120-second default while SEARCH ran
    the very same commands under ``experiment_timeout_s`` (an hour by default).
    Any experiment that took longer than two minutes — which is most of them
    once real training or a large feature extraction is involved — passed SEARCH
    and then failed VALIDATE with a timeout that looked like a broken candidate.

    With the Task B execution API, the held-out target travels on
    ``ExecutionContext.predict_features`` and is materialised onto each
    ``CommandRequest`` by ``ExecutionRuntime.run`` before the backend runs it.
    """
    workdir = Path(workspace.path)
    manifest = read_experiment_manifest(workdir)
    context = ExecutionContext(
        project_root=workdir,
        workspace_root=workdir,
        environment_root=getattr(execution, "environment_root", workdir),
        predict_features=predict_features,
        data_csv=data_csv,
    )
    version = version or "validate"
    try:
        archive_output_roots(workdir, manifest.outputs, version=version)
        for argv in manifest.commands:
            result = await execution.run(
                context,
                CommandRequest(
                    argv=argv,
                    timeout_s=timeout_s,
                    workdir=workdir,
                    emit=publish,
                    predict_features=predict_features,
                    data_csv=data_csv,
                ),
            )
            if not result.ok:
                raise ValidationRunFailed(
                    result.stderr or "validation command failed"
                )
        try:
            assert_output_roots(workdir, manifest.outputs, required={"predictions"})
        except OutputFreshnessError as exc:
            raise PredictionsRejected(str(exc)) from exc
        rel_path = manifest.outputs["predictions"]
        predictions_dir = workdir / rel_path
        if predict_features is not None:
            try:
                assert_predictions_cover(predictions_dir, predict_features)
            except PredictionsCoverageError as exc:
                # 与产物新鲜度一样，这是候选可以自己修的失败，不是编排器的 bug。
                raise PredictionsRejected(str(exc)) from exc
        ref = await pack_directory(store, predictions_dir)
        return PredictionRun(predictions_ref=ref, predictions_path=rel_path)
    finally:
        await git.restore_paths(workspace, tuple(manifest.outputs.values()))


def _run_failure_feedback(
    exc: ValidationRunFailed | PredictionsRejected,
    predict_features: Path | None,
) -> str:
    """Turn a failed re-run into the task text the validate Agent can act on.

    The Agent is created to "repair runtime-only failures", but until this
    existed the repair loop never told it about one: the first crash of the
    frozen command propagated straight out of ``_run_phase``. On 2026-08-31 that
    threw away a five-hour run whose eight SEARCH experiments had all finished,
    over a candidate that hardcoded the SEARCH *label* path while correctly
    reading ``ATHENA_PREDICT_FEATURES`` for the features -- so VALIDATE fed it
    169965 feature rows to score against 169725 search labels.

    Long tracebacks are truncated from the *front*: the exception type and the
    frame that raised it are at the end.
    """
    body = str(exc).strip() or exc.__class__.__name__
    if len(body) > _MAX_FAILURE_FEEDBACK_CHARS:
        body = "[EARLIER FRAMES TRUNCATED]\n" + body[-_MAX_FAILURE_FEEDBACK_CHARS:]
    kind = (
        "The frozen SOTA command failed when re-run"
        if isinstance(exc, ValidationRunFailed)
        else "The frozen SOTA command exited 0 but its predictions were rejected"
    )
    target = (
        f"\n\nATHENA_PREDICT_FEATURES pointed at {predict_features} for this "
        "re-run. That file is the held-out split: it has a different row count "
        "and different row ids than the SEARCH split the candidate was "
        "developed against. Any label file, row count, or index the code holds "
        "fixed alongside it will disagree with it."
        if predict_features is not None
        else ""
    )
    return (
        f"{kind}:\n\n{body}{target}\n\n"
        "Repair the runtime failure only. Do not change the modelling: no new "
        "features, no retuned hyperparameters, no different algorithm, and "
        "nothing that reads held-out labels. The scored artifact must remain "
        "predictions for exactly the rows in ATHENA_PREDICT_FEATURES."
    )


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
    descriptor = EvaluatorDescriptor.model_validate_json(
        await store.get_text(input.final_evaluator_ref)
    )
    evaluator_dir = Path(descriptor.dir_path)
    if not (evaluator_dir / "README.md").is_file():
        raise ValueError(
            "final evaluator directory is missing its README freeze marker"
        )
    predictions = await load_directory(store, current.predictions_ref)
    evaluation = await evaluator.score(
        evaluator_dir=evaluator_dir,
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


class ValidationSession:
    """Stateful run/score/commit phases for one frozen-SOTA validation key."""

    def __init__(
        self,
        input: ValidationInput,
        deps: ValidationDeps,
        options: ValidationOptions,
        result_ref: ArtifactRef | None,
    ) -> None:
        self.input = input
        self.deps = deps
        self.options = options
        self.result_ref = result_ref
        self.current: ValidationResult | None = None

    async def run(self) -> ValidationResult:
        """Recover by durable action and execute the missing validation phases."""
        try:
            await self._verify_key()
            self.current = await _load_result(self.result_ref, self.deps.store)
            action = await recovery_action(
                self.result_ref, self.deps.store, self.input
            )
            if (
                action == "commit"
                and self.current is not None
                and self.current.validation_commit is not None
                and self.current.final_test_score is not None
                and self.current.evidence_ref is not None
            ):
                return self.current

            if action == "run":
                self.current = await self._run_phase()
                action = "score"
            if action == "score":
                self.current = await self._score_phase()
                action = "commit"
            if action == "commit":
                return await self._commit_phase()

            raise RuntimeError(f"unsupported validation recovery action: {action}")
        finally:
            # The validate Agent is a one-shot VALIDATE worker; release it after
            # the phase succeeds or fails so repeated runs do not accumulate.
            try:
                await self.deps.agents.reap(VALIDATE_AGENT_ID)
            except Exception:  # noqa: BLE001,S110 - GC must never mask VALIDATE failure
                pass

    async def _verify_key(self) -> None:
        expected_key = validation_key(
            self.input.sota_commit,
            self.input.reference_metric,
            self.input.direction,
            self.input.final_evaluator_ref,
        )
        if self.input.validation_key != expected_key:
            raise ValueError(
                "validation_key does not match frozen validation inputs"
            )

    async def _run_phase(self) -> ValidationResult:
        """Repair, review, execute, and checkpoint the un-scored prediction run."""
        deps = self.deps
        input = self.input
        options = self.options
        repair = await _decode_repair(deps.agents, deps.store, input=input)
        last_failure: Exception | None = None
        previous_rejection: _ValidationRejectionSignature | None = None
        for _ in range(_MAX_VALIDATION_REPAIR_ATTEMPTS):
            diff = await deps.git.diff(deps.workspace)
            preflight = await _deterministic_preflight(
                workspace=deps.workspace,
                diff=diff,
                explanation=repair.explanation,
                store=deps.store,
            )
            if not preflight.accepted:
                previous_rejection = _record_validation_rejection(
                    previous_rejection,
                    ("preflight", diff.ref, preflight.reason),
                )
                repair = await _decode_repair(
                    deps.agents,
                    deps.store,
                    input=input,
                    feedback=f"Validation policy rejected the repair: {preflight.reason}",
                )
                continue
            review = await review_validation_diff(
                workspace=deps.workspace,
                diff=diff,
                explanation=repair.explanation,
                independent_review=deps.independent_review,
                store=deps.store,
            )
            if not review.accepted:
                previous_rejection = _record_validation_rejection(
                    previous_rejection,
                    ("independent_review", diff.ref, review.reason),
                )
                repair = await _decode_repair(
                    deps.agents,
                    deps.store,
                    input=input,
                    feedback=f"Independent review rejected the repair: {review.reason}",
                )
                continue
            reviewed_diff = diff
            try:
                prediction_run = await _execute_predictions(
                    execution=deps.execution,
                    git=deps.git,
                    workspace=deps.workspace,
                    store=deps.store,
                    publish=deps.publish,
                    timeout_s=options.timeout_s,
                    predict_features=options.predict_features,
                    data_csv=options.data_csv,
                    version=f"validate-{input.validation_key}",
                )
            except (ValidationRunFailed, PredictionsRejected) as exc:
                last_failure = exc
                try:
                    previous_rejection = _record_validation_rejection(
                        previous_rejection,
                        ("execution", diff.ref, str(exc)),
                    )
                except RuntimeError as no_progress:
                    raise no_progress from exc
                repair = await _decode_repair(
                    deps.agents,
                    deps.store,
                    input=input,
                    feedback=_run_failure_feedback(exc, options.predict_features),
                )
                continue
            post_review_diff = await deps.git.diff(deps.workspace)
            if post_review_diff != reviewed_diff:
                previous_rejection = _record_validation_rejection(
                    previous_rejection,
                    (
                        "post_review",
                        post_review_diff.ref,
                        "validation workspace changed after independent review",
                    ),
                )
                repair = await _decode_repair(
                    deps.agents,
                    deps.store,
                    input=input,
                    feedback="Validation workspace changed after independent review",
                )
                continue
            break
        else:
            detail = (
                "validation repair budget exhausted after "
                f"{_MAX_VALIDATION_REPAIR_ATTEMPTS} attempts"
            )
            if last_failure is not None:
                # 不这样做，操作者只会看到「预算耗尽」，而真正的原因——候选脚本
                # 每一次都以同一个 traceback 挂掉——被丢在日志之外。
                detail += f"; last run failure: {last_failure}"
            raise RuntimeError(detail) from last_failure
        review_evidence_ref = await deps.store.put_text(
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
            predictions_ref=prediction_run.predictions_ref,
            predictions_path=prediction_run.predictions_path,
            evidence_ref=review_evidence_ref,
        )
        await _save_result(current, deps.store, deps.checkpoint)
        return current

    async def _score_phase(self) -> ValidationResult:
        """Score packed predictions and checkpoint the completed result."""
        if self.current is None:
            raise RuntimeError("validation recovery result is unavailable")
        self.current = await _score_result(
            input=self.input,
            current=self.current,
            evaluator=self.deps.evaluator,
            store=self.deps.store,
        )
        await _save_result(self.current, self.deps.store, self.deps.checkpoint)
        return self.current

    async def _commit_phase(self) -> ValidationResult:
        """Commit the reviewed workspace and return the finalized validation."""
        if self.current is None:
            raise RuntimeError("validation recovery result is unavailable")
        reviewed_diff = None
        if self.current.evidence_ref is not None:
            try:
                evidence = json.loads(
                    await self.deps.store.get_text(self.current.evidence_ref)
                )
                reviewed_diff = GitDiff(
                    ref=evidence["reviewed_diff_ref"],
                    paths=tuple(evidence["reviewed_diff_paths"]),
                )
            except (KeyError, TypeError, ValueError):
                reviewed_diff = None
        if reviewed_diff is None:
            raise RuntimeError("validation commit requires reviewed diff evidence")
        diff = await self.deps.git.diff(self.deps.workspace)
        if diff != reviewed_diff:
            raise RuntimeError(
                "validation workspace changed after independent review"
            )
        commit = await self.deps.git.commit(
            self.deps.workspace,
            diff,
            f"validate frozen SOTA {self.input.sota_commit}",
        )
        self.current = self.current.model_copy(update={"validation_commit": commit})
        await _save_result(self.current, self.deps.store, self.deps.checkpoint)
        return self.current


async def run_validation_plan(
    input: ValidationInput,
    deps: ValidationDeps,
    options: ValidationOptions,
    result_ref: ArtifactRef | None,
) -> ValidationResult:
    """Run or recover one independent validation attempt under its stable key.

    ``options.timeout_s`` is the same budget SEARCH gave the experiment; the
    re-run must not be held to a stricter one than the run it is reproducing.

    ``options.predict_features`` is the held-out feature file this phase exists
    to score against. It is exported as ``ATHENA_PREDICT_FEATURES`` for the
    length of the re-run only. Without it the re-run reproduces the *search*
    predictions -- which is what a platform-split project did until 2026-08-30,
    so the final evaluator saw nothing but unknown row ids and reported
    ``{"primary": 0.0}``.
    """
    session = ValidationSession(
        input=input,
        deps=deps,
        options=options,
        result_ref=result_ref,
    )
    return await session.run()


__all__ = [
    "CheckpointValidation",
    "PredictionRun",
    "PredictionsRejected",
    "ValidationDeps",
    "ValidationDiffReview",
    "ValidationInput",
    "ValidationOptions",
    "ValidationRunFailed",
    "ValidationSession",
    "recovery_action",
    "review_validation_diff",
    "run_validation_plan",
    "validation_key",
]

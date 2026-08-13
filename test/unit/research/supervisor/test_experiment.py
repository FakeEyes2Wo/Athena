"""Experiment manifest validation and trusted patience logic tests."""

import json
import shutil
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.execution.runtime import CommandResult, ExecutionContext
from athena.research.contracts import CandidateEvaluation, DataScriptBundle
from athena.research.script_runner import load_directory
from athena.research.supervisor.experiment import (
    ExperimentManifest,
    PlanRunner,
    PlanTurnResult,
    apply_trusted_score,
    decide_settlement,
    load_best,
)
from athena.research.supervisor.plans import (
    PlanBest,
    PlanDecision,
    PlanInput,
    PlanState,
)

_REF = "sha256:" + "a" * 64
_OTHER_REF = "sha256:" + "c" * 64


def _decision(value: str) -> PlanDecision:
    return PlanDecision.model_validate(
        {"decision": value, "reason": f"test decision: {value}"}
    )


def test_scored_turn_result_requires_next_state() -> None:
    with pytest.raises(ValidationError, match="next_state"):
        PlanTurnResult(kind="scored")


def test_failed_turn_result_rejects_state_payload() -> None:
    state = PlanState(kind="PREPARE", context_ref=_REF, turns_used=0, turn_limit=12)
    for state_payload in (
        {"next_state": state},
        {"stale_rounds": 0},
        {"best_ref": _OTHER_REF},
    ):
        with pytest.raises(ValidationError):
            PlanTurnResult(kind="execution_failed", error="boom", **state_payload)


class _FakeExecution:
    """Minimal ExecutionRuntime double returning canned command results."""

    def __init__(self, results: list[CommandResult]) -> None:
        self._results = results
        self.calls: list[list[str]] = []
        self.workdirs: list[str | None] = []
        self.emit_seen: list[object] = []

    async def run(
        self,
        context: ExecutionContext,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        timeout_s: int = 120,
        workdir: str | None = None,
        emit=None,
    ) -> CommandResult:
        self.calls.append(list(argv) if argv is not None else [])
        self.workdirs.append(workdir)
        if emit is not None:
            emit("command/started", "exec:run", {"command": argv})
            self.emit_seen.append(emit)
        return self._results.pop(0)


class _FakeWorkspace:
    """Minimal GitWorkspace double committing a fixed sequence of hashes."""

    def __init__(self, commits: list[str]) -> None:
        self._commits = iter(commits)
        self.diffs: list[GitDiff] = []
        self.messages: list[str] = []

    async def diff(self, workspace: GitWorkBranch) -> GitDiff:
        diff = GitDiff(ref=_OTHER_REF, paths=("experiment.json",))
        self.diffs.append(diff)
        return diff

    async def commit(
        self, workspace: GitWorkBranch, approved_diff: GitDiff, message: str
    ) -> str:
        self.messages.append(message)
        return next(self._commits)


class _FakeEvaluator:
    """Minimal TrustedEvaluator double returning a canned metric."""

    def __init__(self, metric: float = 0.9, *, error: Exception | None = None) -> None:
        self._metric = metric
        self._error = error

    async def score(
        self,
        *,
        eval_bundle: DataScriptBundle,
        predictions: dict[str, bytes],
        candidate_id: str,
        direction: str,
        predictions_root: str = "predictions.csv",
        predictions_path: str = "predictions.csv",  # 兼容旧调用，Task 3/4 落定后移除
    ) -> CandidateEvaluation:
        if self._error is not None:
            raise self._error
        return CandidateEvaluation(
            candidate_id=candidate_id,
            test_score=self._metric,
            direction=direction,
        )


async def _scored_plan(
    store: LocalArtifactStore, *, best_metric: float, stale_rounds: int, patience: int
) -> PlanState:
    best = PlanBest(metric=best_metric, commit="c1", evidence_ref=_OTHER_REF)
    best_ref = await store.put_text(best.model_dump_json())
    return PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=patience,
        stale_rounds=stale_rounds,
        best_ref=best_ref,
    )


async def _runner_setup(
    tmp_path: Path, *, execution: _FakeExecution, evaluator: _FakeEvaluator
):
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = DataScriptBundle(bundle_id="b1", entrypoint="eval.py")
    evaluator_ref = await store.put_text(bundle.model_dump_json())
    plan_input = PlanInput(evaluator_ref=evaluator_ref, tree_ref=_OTHER_REF)
    branch = GitWorkBranch(
        path=str(tmp_path / "ws"), branch="athena/plan/h1", base_commit="c0"
    )
    Path(branch.path).mkdir(parents=True, exist_ok=True)
    workspace = _FakeWorkspace(["c1"])
    runner = PlanRunner(
        execution=execution,
        store=store,
        evaluator=evaluator,
        workspace=workspace,
        branch=branch,
        context=ExecutionContext(
            project_root=tmp_path,
            workspace_root=Path(branch.path),
            environment_root=tmp_path,
        ),
    )
    return runner, plan_input, workspace, branch, store


def _write_manifest(
    branch: GitWorkBranch,
    *,
    commands: list[list[str]],
    outputs: dict[str, str] | None = None,
    predictions: str = "id,pred\n",
) -> None:
    workdir = Path(branch.path)
    outputs = outputs or {
        "predictions": "outputs/predictions",
        "report": "report.md",
    }
    (workdir / "experiment.json").write_text(
        json.dumps({"version": 1, "commands": commands, "outputs": outputs}),
        encoding="utf-8",
    )
    predictions_dir = workdir / outputs["predictions"]
    predictions_dir.mkdir(parents=True, exist_ok=True)
    (predictions_dir / "predictions.csv").write_bytes(predictions.encode("utf-8"))
    (workdir / "report.md").write_text("# report\n", encoding="utf-8")


def test_manifest_rejects_shell_strings_and_path_escape() -> None:
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            {
                "version": 1,
                "commands": ["python train.py"],
                "outputs": {"predictions": "../labels.csv", "report": "report.md"},
            }
        )


def test_manifest_rejects_shell_string_command() -> None:
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            {
                "version": 1,
                "commands": [["python", "train.py"], "uv run predict.py"],
                "outputs": {"predictions": "outputs/predictions.csv"},
            }
        )


def test_manifest_rejects_escaping_output_path() -> None:
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            {
                "version": 1,
                "commands": [["python", "train.py"]],
                "outputs": {"predictions": "../labels.csv"},
            }
        )


def test_manifest_rejects_absolute_output_path() -> None:
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            {
                "version": 1,
                "commands": [["python", "train.py"]],
                "outputs": {"predictions": str(Path("/tmp/abs.csv"))},
            }
        )


def test_manifest_rejects_git_command() -> None:
    with pytest.raises(ValidationError, match="git"):
        ExperimentManifest.model_validate(
            {
                "version": 1,
                "commands": [["git", "status"]],
                "outputs": {"predictions": "outputs/predictions.csv"},
            }
        )


def test_manifest_rejects_unsupported_version() -> None:
    with pytest.raises(ValidationError, match="version"):
        ExperimentManifest.model_validate(
            {
                "version": 2,
                "commands": [["python", "train.py"]],
                "outputs": {"predictions": "outputs/predictions.csv"},
            }
        )


def test_manifest_requires_predictions_output() -> None:
    with pytest.raises(ValidationError, match="predictions"):
        ExperimentManifest.model_validate(
            {
                "version": 1,
                "commands": [["python", "train.py"]],
                "outputs": {"report": "report.md"},
            }
        )


def test_manifest_rejects_empty_or_blank_command() -> None:
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            {
                "version": 1,
                "commands": [[]],
                "outputs": {"predictions": "outputs/predictions.csv"},
            }
        )


def test_manifest_accepts_argv_manifest() -> None:
    manifest = ExperimentManifest.model_validate(
        {
            "version": 1,
            "commands": [["uv", "run", "python", "-m", "solution.train"]],
            "outputs": {
                "predictions": "outputs/predictions.csv",
                "report": "outputs/report.md",
            },
        }
    )

    assert manifest.commands[0] == ["uv", "run", "python", "-m", "solution.train"]
    assert manifest.outputs["predictions"] == "outputs/predictions.csv"


@pytest.mark.asyncio
async def test_trusted_improvement_resets_patience_and_keeps_historical_best(
    tmp_path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    state = await _scored_plan(store, best_metric=0.80, stale_rounds=2, patience=3)

    improved = await apply_trusted_score(
        state, metric=0.82, commit="c2", store=store, evidence_ref=_OTHER_REF
    )
    assert improved.stale_rounds == 0

    worse = await apply_trusted_score(
        improved, metric=0.81, commit="c3", store=store, evidence_ref=_OTHER_REF
    )
    assert worse.stale_rounds == 1

    best = await load_best(worse.best_ref, store)
    assert best.commit == "c2"
    assert best.metric == 0.82


@pytest.mark.asyncio
async def test_first_trusted_score_sets_best_and_zeroes_stale(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=3,
    )

    scored = await apply_trusted_score(
        state, metric=0.84, commit="c1", store=store, evidence_ref=_OTHER_REF
    )

    assert scored.stale_rounds == 0
    assert scored.best_ref is not None
    best = await load_best(scored.best_ref, store)
    assert best.commit == "c1"
    assert best.metric == 0.84


@pytest.mark.asyncio
async def test_minimize_direction_counts_lower_as_improvement(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    state = await _scored_plan(store, best_metric=0.30, stale_rounds=1, patience=3)

    improved = await apply_trusted_score(
        state,
        metric=0.25,
        commit="c2",
        store=store,
        evidence_ref=_OTHER_REF,
        direction="minimize",
    )
    assert improved.stale_rounds == 0

    worse = await apply_trusted_score(
        improved,
        metric=0.27,
        commit="c3",
        store=store,
        evidence_ref=_OTHER_REF,
        direction="minimize",
    )
    assert worse.stale_rounds == 1
    assert (await load_best(worse.best_ref, store)).commit == "c2"


def test_submit_settles_historical_best() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=3,
        turn_limit=12,
        patience=4,
        best_ref=_OTHER_REF,
    )

    settlement = decide_settlement(
        state,
        PlanDecision(decision="submit", reason="trusted result is ready"),
        report_ref=_REF,
    )

    assert settlement.action == "settle"
    assert settlement.best_ref == _OTHER_REF


def test_final_settlement_waits_for_report() -> None:
    state = PlanState(
        kind="PREPARE",
        context_ref=_REF,
        turns_used=3,
        turn_limit=12,
    )

    settlement = decide_settlement(
        state, PlanDecision(decision="submit", reason="done")
    )

    assert settlement.action == "wait"
    assert settlement.reason == "report required before settlement"


def test_search_settles_without_report() -> None:
    """SEARCH 的 report 输出是可选的，缺失不应把 settle 卡成 wait。"""
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=3,
        turn_limit=12,
        patience=4,
        best_ref=_OTHER_REF,
    )

    settlement = decide_settlement(
        state, PlanDecision(decision="submit", reason="done")
    )

    assert settlement.action == "settle"


@pytest.mark.parametrize("decision", ["submit", "abandon"])
def test_every_final_settlement_waits_for_report(decision: str) -> None:
    """PREPARE 的 submit/abandon 在 report 缺失时都必须改为 wait。"""
    state = PlanState(
        kind="PREPARE",
        context_ref=_REF,
        turns_used=3,
        turn_limit=12,
    )

    settlement = decide_settlement(state, _decision(decision))

    assert settlement.action == "wait"
    assert settlement.reason == "report required before settlement"


def test_patience_exhaustion_settles_historical_best() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=2,
        turn_limit=12,
        patience=3,
        stale_rounds=3,
        best_ref=_OTHER_REF,
    )

    settlement = decide_settlement(state, _decision("continue"), report_ref=_REF)

    assert settlement.action == "settle"
    assert settlement.best_ref == _OTHER_REF


def test_turn_exhaustion_with_best_settles() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=12,
        turn_limit=12,
        patience=4,
        best_ref=_OTHER_REF,
    )

    settlement = decide_settlement(state, _decision("continue"), report_ref=_REF)

    assert settlement.action == "settle"
    assert settlement.best_ref == _OTHER_REF


def test_turn_exhaustion_without_best_waits() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=12,
        turn_limit=12,
        patience=4,
    )

    settlement = decide_settlement(state, _decision("continue"))

    assert settlement.action == "wait"
    assert settlement.best_ref is None


def test_continue_with_budget_keeps_running() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=4,
        turn_limit=12,
        patience=4,
        stale_rounds=1,
        best_ref=_OTHER_REF,
    )

    settlement = decide_settlement(state, _decision("continue"))

    assert settlement.action == "continue"


def test_abandon_without_best_settles_loss() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=5,
        turn_limit=12,
        patience=4,
    )

    settlement = decide_settlement(state, _decision("abandon"), report_ref=_REF)

    assert settlement.action == "settle"
    assert settlement.best_ref is None


def test_abandon_with_best_settles_best() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=5,
        turn_limit=12,
        patience=4,
        best_ref=_OTHER_REF,
    )

    settlement = decide_settlement(state, _decision("abandon"), report_ref=_REF)

    assert settlement.action == "settle"
    assert settlement.best_ref == _OTHER_REF


def test_unlimited_turns_never_exhaust() -> None:
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=99,
        turn_limit=None,
        patience=4,
        stale_rounds=1,
        best_ref=_OTHER_REF,
    )

    settlement = decide_settlement(state, _decision("continue"))

    assert settlement.action == "continue"


@pytest.mark.asyncio
async def test_run_turn_executes_manifest_scores_and_commits(tmp_path) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, workspace, branch, store = await _runner_setup(
        tmp_path, execution=execution, evaluator=_FakeEvaluator(metric=0.91)
    )
    _write_manifest(
        branch,
        commands=[[sys.executable, "-c", "print('train')"]],
        predictions="__athena_row_id,prediction\nrow_1,1\nrow_2,0\n",
    )
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == "scored"
    assert result.metric == 0.91
    assert result.commit == "c1"
    assert result.next_state is not None
    assert result.next_state.stale_rounds == 0
    assert result.next_state.best_ref is not None
    best = await load_best(result.next_state.best_ref, store)
    assert best.commit == "c1"
    assert best.metric == 0.91
    assert workspace.messages == ["plan h1 trusted score 0.9100"]
    assert execution.calls == [[sys.executable, "-c", "print('train')"]]
    assert execution.workdirs == [str(Path(branch.path))]
    assert result.predictions_ref is not None
    predictions_tree = await load_directory(store, result.predictions_ref)
    assert predictions_tree == {
        "predictions.csv": b"__athena_row_id,prediction\nrow_1,1\nrow_2,0\n"
    }


@pytest.mark.asyncio
async def test_run_turn_forwards_emit_callbacks(tmp_path) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, _, branch, _ = await _runner_setup(
        tmp_path, execution=execution, evaluator=_FakeEvaluator(metric=0.91)
    )
    _write_manifest(branch, commands=[[sys.executable, "-c", "print('train')"]])
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
    )
    seen: list[tuple[str, str, object]] = []

    def emit(kind: str, ref: str, data: object) -> None:
        seen.append((kind, ref, data))

    await runner.run_turn("h1", state, plan_input, emit=emit)

    assert seen == [
        (
            "command/started",
            "exec:run",
            {"command": [sys.executable, "-c", "print('train')"]},
        )
    ]
    assert execution.emit_seen


@pytest.mark.asyncio
async def test_run_turn_execution_failure_does_not_increment_stale(tmp_path) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=False, stdout="", stderr="boom", exit_code=1)]
    )
    runner, plan_input, workspace, branch, _ = await _runner_setup(
        tmp_path, execution=execution, evaluator=_FakeEvaluator(metric=0.91)
    )
    _write_manifest(branch, commands=[[sys.executable, "train.py"]])
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
        stale_rounds=2,
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == "execution_failed"
    assert result.next_state is None
    assert workspace.messages == []
    assert state.stale_rounds == 2


@pytest.mark.asyncio
async def test_run_turn_missing_predictions_returns_evidence_without_scoring(
    tmp_path,
) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, workspace, branch, store = await _runner_setup(
        tmp_path, execution=execution, evaluator=_FakeEvaluator(metric=0.91)
    )
    _write_manifest(branch, commands=[[sys.executable, "predict.py"]])
    # remove the predictions directory the manifest promises to produce
    shutil.rmtree(Path(branch.path) / "outputs" / "predictions")
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
        stale_rounds=2,
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == "output_failed"
    assert result.next_state is None
    assert result.evidence_ref is not None
    evidence = json.loads(await store.get_text(result.evidence_ref))
    assert evidence["kind"] == "output_failed"
    assert evidence["plan"] == "h1"
    assert workspace.messages == []
    assert state.stale_rounds == 2


@pytest.mark.asyncio
async def test_run_turn_scoring_failure_does_not_increment_stale(tmp_path) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, workspace, branch, store = await _runner_setup(
        tmp_path,
        execution=execution,
        evaluator=_FakeEvaluator(error=ValueError("row ids do not align")),
    )
    _write_manifest(branch, commands=[[sys.executable, "predict.py"]])
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
        stale_rounds=2,
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == "scoring_failed"
    assert result.next_state is None
    assert result.predictions_ref is not None
    assert workspace.messages == []
    assert state.stale_rounds == 2
    predictions = await load_directory(store, result.predictions_ref)
    assert predictions == {"predictions.csv": b"id,pred\n"}


@pytest.mark.asyncio
async def test_run_turn_evaluator_infrastructure_failure_is_retryable(tmp_path) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, workspace, branch, _ = await _runner_setup(
        tmp_path,
        execution=execution,
        evaluator=_FakeEvaluator(error=RuntimeError("provider unavailable")),
    )
    _write_manifest(branch, commands=[[sys.executable, "predict.py"]])
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
        stale_rounds=2,
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == "evaluator_infrastructure_failed"
    assert result.next_state is None
    assert result.predictions_ref is not None
    assert workspace.messages == []
    assert state.stale_rounds == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (ValueError("row ids do not align; api_key=private-value"), "scoring_failed"),
        (
            RuntimeError("evaluator unavailable; access_token=private-value"),
            "evaluator_infrastructure_failed",
        ),
    ],
)
async def test_run_turn_redacts_evaluator_failure_details(
    tmp_path, error: Exception, expected_kind: str
) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, _, branch, store = await _runner_setup(
        tmp_path,
        execution=execution,
        evaluator=_FakeEvaluator(error=error),
    )
    _write_manifest(branch, commands=[[sys.executable, "predict.py"]])
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
        stale_rounds=2,
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == expected_kind
    assert result.error is not None
    assert "private-value" not in result.error
    assert "[REDACTED]" in result.error
    assert result.evidence_ref is not None
    evidence = json.loads(await store.get_text(result.evidence_ref))
    assert "private-value" not in evidence["error"]
    assert "[REDACTED]" in evidence["error"]
    assert state.stale_rounds == 2


@pytest.mark.asyncio
async def test_run_turn_invalid_manifest_is_rejected(tmp_path) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, workspace, branch, store = await _runner_setup(
        tmp_path, execution=execution, evaluator=_FakeEvaluator()
    )
    secret_command = "private-command-token"
    (Path(branch.path) / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": secret_command,
                "outputs": {"predictions": "outputs/predictions.csv"},
            }
        ),
        encoding="utf-8",
    )
    state = PlanState(
        kind="SEARCH",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
        patience=4,
    )

    result = await runner.run_turn("h1", state, plan_input)

    assert result.kind == "manifest_invalid"
    assert result.next_state is None
    assert result.error is not None
    assert "commands" in result.error
    assert "valid array" in result.error
    assert secret_command not in result.error
    assert str(Path(branch.path)) not in result.error
    assert result.evidence_ref is not None
    evidence = json.loads(await store.get_text(result.evidence_ref))
    assert evidence["kind"] == "manifest_invalid"
    assert evidence["error"] == result.error
    assert secret_command not in evidence["error"]
    assert str(Path(branch.path)) not in evidence["error"]
    assert execution.calls == []
    assert workspace.messages == []


@pytest.mark.asyncio
async def test_prepare_requires_report_before_scoring(tmp_path) -> None:
    execution = _FakeExecution(
        [CommandResult(ok=True, stdout="ok", stderr="", exit_code=0)]
    )
    runner, plan_input, workspace, branch, store = await _runner_setup(
        tmp_path, execution=execution, evaluator=_FakeEvaluator(metric=0.91)
    )
    _write_manifest(
        branch,
        commands=[[sys.executable, "predict.py"]],
        outputs={"predictions": "outputs/predictions"},
    )
    state = PlanState(
        kind="PREPARE",
        context_ref=_REF,
        turns_used=1,
        turn_limit=12,
    )

    result = await runner.run_turn("prepare", state, plan_input)

    assert result.kind == "output_failed"
    assert result.next_state is None
    assert result.evidence_ref is not None
    assert "report" in json.loads(await store.get_text(result.evidence_ref))["error"]
    assert workspace.messages == []

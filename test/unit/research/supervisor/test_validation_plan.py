import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.research.contracts import ValidationResult
from athena.research.script_runner import load_directory
from athena.research.supervisor import validation as validation_module
from athena.research.supervisor.validation import (
    PredictionsRejected,
    ValidationDiffReview,
    ValidationInput,
    ValidationRunFailed,
    _execute_predictions,
    _MAX_FAILURE_FEEDBACK_CHARS,
    _run_failure_feedback,
    drop_output_sections,
    recovery_action,
    review_validation_diff,
    validation_key,
)


def test_validation_stops_when_the_same_rejection_makes_no_progress() -> None:
    signature = ("preflight", "sha256:diff-a", "policy rejection")

    previous = validation_module._record_validation_rejection(None, signature)

    with pytest.raises(RuntimeError, match="validation repair made no progress"):
        validation_module._record_validation_rejection(previous, signature)


def test_validation_allows_a_changed_diff_after_rejection() -> None:
    previous = validation_module._record_validation_rejection(
        None, ("execution", "sha256:diff-a", "candidate failed")
    )

    current = validation_module._record_validation_rejection(
        previous, ("execution", "sha256:diff-b", "candidate failed")
    )

    assert current == ("execution", "sha256:diff-b", "candidate failed")


def test_run_failure_feedback_keeps_the_end_of_a_long_traceback() -> None:
    """The exception type and the raising frame are at the tail, not the head."""
    noise = "\n".join(f'  File "f{i}.py", line {i}, in main' for i in range(4000))
    exc = ValidationRunFailed(noise + "\nValueError: inconsistent numbers of samples")

    feedback = _run_failure_feedback(exc, Path("final_features.csv"))

    assert "ValueError: inconsistent numbers of samples" in feedback
    assert "[EARLIER FRAMES TRUNCATED]" in feedback
    assert 'File "f0.py"' not in feedback
    assert len(feedback) < len(noise)


def test_run_failure_feedback_names_the_held_out_target_and_forbids_remodelling() -> (
    None
):
    """The Agent must be able to see *why* a fixed row count disagrees."""
    feedback = _run_failure_feedback(
        ValidationRunFailed("boom"), Path("/split/final_features.csv")
    )

    assert "final_features.csv" in feedback
    assert "different row count" in feedback
    assert "Do not change the modelling" in feedback


def test_rejected_predictions_are_described_as_a_clean_exit() -> None:
    """A zero exit with unusable output is a different repair than a crash."""
    feedback = _run_failure_feedback(PredictionsRejected("no rows overlap"), None)

    assert "exited 0 but its predictions were rejected" in feedback
    assert "no rows overlap" in feedback
    # 没有 predict_features 时不能凭空编造一个路径。
    assert "ATHENA_PREDICT_FEATURES pointed at" not in feedback
    assert _MAX_FAILURE_FEEDBACK_CHARS > 0


def test_validation_key_is_stable_and_input_sensitive() -> None:
    first = validation_key("sota-a", 0.82, "maximize", "final-eval-a")

    assert first == validation_key("sota-a", 0.82, "maximize", "final-eval-a")
    assert first != validation_key("sota-b", 0.82, "maximize", "final-eval-a")
    assert first != validation_key("sota-a", 0.81, "maximize", "final-eval-a")
    assert first != validation_key("sota-a", 0.82, "minimize", "final-eval-a")
    assert first != validation_key("sota-a", 0.82, "maximize", "final-eval-b")


def test_validation_input_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        ValidationInput.model_validate(
            {
                "sota_commit": "sota-a",
                "reference_metric": 0.82,
                "direction": "maximize",
                "final_evaluator_ref": "final-eval-a",
                "validation_key": "key-a",
                "unexpected": True,
            }
        )


@pytest.mark.asyncio
async def test_complete_result_commits_without_rescoring(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    validation_input = ValidationInput(
        sota_commit="sota-a",
        reference_metric=0.82,
        direction="maximize",
        final_evaluator_ref="final-eval-a",
        validation_key=validation_key("sota-a", 0.82, "maximize", "final-eval-a"),
    )
    evidence_ref = await store.put_text(
        json.dumps(
            {
                "reviewed_diff_ref": "diff-a",
                "reviewed_diff_paths": ["solution/runtime.py"],
            }
        )
    )
    result = ValidationResult(
        result_id=validation_input.validation_key,
        status="COMPLETED",
        test_score=0.82,
        final_test_score=0.80,
        generalization_gap=0.02,
        generalization_warning=True,
        sota_commit="sota-a",
        validation_commit=None,
        predictions_ref="predictions-a",
        evidence_ref=evidence_ref,
    )
    result_ref = await store.put_text(result.model_dump_json())

    assert await recovery_action(result_ref, store, validation_input) == "commit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, "run"),
        ("not-json", "run"),
        (
            json.dumps(
                {
                    "result_id": "validate-key",
                    "status": "COMPLETED",
                    "sota_commit": "sota-a",
                    "predictions_ref": "predictions-a",
                }
            ),
            "run",
        ),
    ],
)
async def test_recovery_action_requires_matching_reviewed_diff_evidence(
    tmp_path, payload: str | None, expected: str
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    result_ref = await store.put_text(payload) if payload is not None else None
    validation_input = ValidationInput(
        sota_commit="sota-a",
        reference_metric=0.82,
        direction="maximize",
        final_evaluator_ref="final-eval-a",
        validation_key=validation_key("sota-a", 0.82, "maximize", "final-eval-a"),
    )

    assert await recovery_action(result_ref, store, validation_input) == expected


@pytest.mark.asyncio
async def test_scored_recovery_rejects_missing_reviewed_diff_evidence(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    validation_input = ValidationInput(
        sota_commit="sota-a",
        reference_metric=0.82,
        direction="maximize",
        final_evaluator_ref="final-eval-a",
        validation_key=validation_key("sota-a", 0.82, "maximize", "final-eval-a"),
    )
    result_ref = await store.put_text(
        ValidationResult(
            result_id=validation_input.validation_key,
            status="COMPLETED",
            test_score=0.82,
            final_test_score=0.79,
            sota_commit="sota-a",
            predictions_ref="predictions-a",
        ).model_dump_json()
    )

    with pytest.raises(RuntimeError, match="reviewed diff evidence"):
        await recovery_action(result_ref, store, validation_input)


async def _validation_branch(tmp_path, path: str, content: str = ""):
    workspace = tmp_path / "validate"
    changed = workspace / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text(content, encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    diff_ref = await store.put_bytes(
        f"diff --git a/{path} b/{path}\n+++ b/{path}\n+{content}".encode()
    )
    return store, (
        GitWorkBranch(path=str(workspace), branch="validate", base_commit="sota-a"),
        GitDiff(ref=diff_ref, paths=(path,)),
    )


def _write_manifest_for_review(workspace: Path) -> None:
    """The manifest the review reads to learn which paths are outputs."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "solution/train_model.py"]],
                "outputs": {"predictions": "predictions", "report": "REPORT.md"},
            }
        ),
        encoding="utf-8",
    )


def _diff_section(path: str, body: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{body}\n"


@pytest.mark.asyncio
async def test_review_hides_the_outputs_the_agent_rewrote_while_testing(
    tmp_path,
) -> None:
    """The Agent must run the command to test its repair; that rewrites outputs.

    On 2026-09-02 the reviewer rejected a correct repair because the diff carried
    the regenerated predictions file: "replacing the entire prediction file
    implies a change in the model's inference results". Worse, ``predictions/``
    sorts before ``solution/``, so a 169,965-row CSV pushed the source change
    past the review's character bound — the reviewer was rejecting a repair it
    could not see.
    """
    workspace_dir = tmp_path / "validate"
    _write_manifest_for_review(workspace_dir)
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    huge = "\n".join(f"+{i},0.5,0" for i in range(50_000))
    raw = (
        _diff_section("predictions/predictions.csv", huge)
        + _diff_section("REPORT.md", "+Search PR-AUC: 0.8361")
        + _diff_section("solution/train_model.py", "+SEARCH_LABELS_PATH = None")
    ).encode()
    diff = GitDiff(
        ref=await store.put_bytes(raw),
        paths=("REPORT.md", "predictions/predictions.csv", "solution/train_model.py"),
    )
    seen = ""

    async def reviewer(prompt: str) -> ValidationDiffReview:
        nonlocal seen
        seen = prompt
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="drop the hardcoded search label path",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is True
    payload = json.loads(seen)
    assert payload["paths"] == ["solution/train_model.py"]
    assert "SEARCH_LABELS_PATH = None" in payload["changed_text"]
    assert "predictions/predictions.csv" not in payload["changed_text"]
    assert "REPORT.md" not in payload["changed_text"]
    assert "[DIFF TRUNCATED]" not in payload["changed_text"]
    # 评审必须知道产物是被有意剔除的，否则会以为候选压根没改代码。
    assert "excluded_outputs" in payload


@pytest.mark.asyncio
async def test_review_says_nothing_about_exclusions_when_only_source_changed(
    tmp_path,
) -> None:
    workspace_dir = tmp_path / "validate"
    _write_manifest_for_review(workspace_dir)
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    diff = GitDiff(
        ref=await store.put_bytes(
            _diff_section("solution/runtime.py", "+SEED = 7").encode()
        ),
        paths=("solution/runtime.py",),
    )
    seen = ""

    async def reviewer(prompt: str) -> ValidationDiffReview:
        nonlocal seen
        seen = prompt
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="make the seed deterministic",
        independent_review=reviewer,
        store=store,
    )

    assert "excluded_outputs" not in json.loads(seen)


@pytest.mark.asyncio
async def test_a_binary_declared_output_no_longer_blocks_a_text_repair(
    tmp_path,
) -> None:
    """A pickled model beside the predictions used to fail the whole review."""
    workspace_dir = tmp_path / "validate"
    _write_manifest_for_review(workspace_dir)
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    raw = (
        b"diff --git a/predictions/model.pkl b/predictions/model.pkl\n"
        b"GIT binary patch\n\x00\x01\x02\n"
        + _diff_section("solution/runtime.py", "+SEED = 7").encode()
    )
    diff = GitDiff(
        ref=await store.put_bytes(raw),
        paths=("predictions/model.pkl", "solution/runtime.py"),
    )

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="make the seed deterministic",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is True


@pytest.mark.asyncio
async def test_editing_the_manifest_forfeits_the_output_exclusion(tmp_path) -> None:
    """Otherwise "declare solution an output" hides the source change.

    The manifest read for the exclusion lives in the Agent's own worktree, so
    the exclusion has to cost the edit that would abuse it.
    """
    workspace_dir = tmp_path / "validate"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "solution/train_model.py"]],
                "outputs": {"predictions": "solution"},
            }
        ),
        encoding="utf-8",
    )
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    raw = (
        _diff_section("experiment.json", '+  "outputs": {"predictions": "solution"}')
        + _diff_section("solution/train_model.py", "+LEARNING_RATE = 0.9")
    ).encode()
    diff = GitDiff(
        ref=await store.put_bytes(raw),
        paths=("experiment.json", "solution/train_model.py"),
    )
    reviewer_called = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewer_called
        reviewer_called = True
        return ValidationDiffReview(accepted=True, reason="looks fine")

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="runtime path repair",
        independent_review=reviewer,
        store=store,
    )

    # 产物没有被排除，所以改动的 learning_rate 仍然撞上确定性语义闸门。
    assert result.accepted is False
    assert reviewer_called is False


@pytest.mark.asyncio
async def test_a_generated_report_mentioning_a_marker_is_not_a_semantic_change(
    tmp_path,
) -> None:
    """The 2026-09-02 VALIDATE, in miniature.

    The validate Agent ran the frozen command from inside ``solution/``, so its
    report landed at ``solution/REPORT.md`` -- outside the declared outputs, and
    that report *describes* the model it ran: "learning_rate: 0.05". The gate
    scanned the whole diff text, matched the marker, and answered "validation
    cannot change model or training semantics" 16 times in a row. Nothing the
    Agent could write would have removed a word from a report the frozen command
    regenerates.
    """
    workspace_dir = tmp_path / "validate"
    _write_manifest_for_review(workspace_dir)
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    raw = (
        _diff_section("solution/REPORT.md", "+- learning_rate: 0.05\n+- num_leaves: 31")
        + _diff_section("solution/train_model.py", "+PREDICTIONS_DIR = 'predictions'")
    ).encode()
    diff = GitDiff(
        ref=await store.put_bytes(raw),
        paths=("solution/REPORT.md", "solution/train_model.py"),
    )
    reviewed = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewed
        reviewed = True
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="point the output directory at this workspace",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is True
    assert reviewed is True


@pytest.mark.asyncio
async def test_a_marker_in_context_no_longer_rejects_the_edit(tmp_path) -> None:
    """Git carries three lines of context; a marker in them is not the edit.

    A LightGBM solution has ``learning_rate`` in its parameter dict, so a pure
    path repair a few lines away was unfixable — the marker was in code the Agent
    never touched.
    """
    workspace_dir = tmp_path / "validate"
    _write_manifest_for_review(workspace_dir)
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    body = (
        " params = {\n"
        "     'learning_rate': 0.05,\n"
        "-PREDICTIONS_DIR = '/other/workspace/predictions'\n"
        "+PREDICTIONS_DIR = 'predictions'\n"
        " }"
    )
    diff = GitDiff(
        ref=await store.put_bytes(_diff_section("solution/run.py", body).encode()),
        paths=("solution/run.py",),
    )

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="point the output directory at this workspace",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is True


@pytest.mark.asyncio
async def test_retuning_a_hyperparameter_is_still_rejected_and_named(tmp_path) -> None:
    """The gate must still do its job, and now say what tripped it."""
    workspace_dir = tmp_path / "validate"
    _write_manifest_for_review(workspace_dir)
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    body = "-    'learning_rate': 0.05,\n+    'learning_rate': 0.2,"
    diff = GitDiff(
        ref=await store.put_bytes(_diff_section("solution/run.py", body).encode()),
        paths=("solution/run.py",),
    )
    reviewer_called = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewer_called
        reviewer_called = True
        return ValidationDiffReview(accepted=True, reason="looks fine")

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="tune the model",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False
    assert reviewer_called is False
    # 必须点名，否则 Agent 只能原样再交一次（真机上连交了 16 次）。
    assert "learning_rate" in result.reason


@pytest.mark.asyncio
async def test_final_label_access_is_still_rejected_even_in_an_artifact(
    tmp_path,
) -> None:
    """Leakage keeps scanning artifacts; only the semantic gate skips them."""
    workspace_dir = tmp_path / "validate"
    _write_manifest_for_review(workspace_dir)
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    diff = GitDiff(
        ref=await store.put_bytes(
            _diff_section("solution/scores.csv", "+id,final_label\n+1,1").encode()
        ),
        paths=("solution/scores.csv",),
    )

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="write scores",
        independent_review=None,  # 不该走到评审
        store=store,
    )

    assert result.accepted is False
    assert "final_label" in result.reason


def test_dropping_output_sections_leaves_a_diff_without_outputs_alone() -> None:
    raw = _diff_section("solution/a.py", "+x = 1").encode()

    assert drop_output_sections(raw, ("predictions", "REPORT.md")) == raw
    assert drop_output_sections(raw, ()) == raw


def test_dropping_output_sections_matches_whole_segments_only() -> None:
    """`predictions_backup/` is not inside `predictions/`."""
    raw = (
        _diff_section("predictions_backup/old.csv", "+1,2")
        + _diff_section("predictions/predictions.csv", "+3,4")
    ).encode()

    kept = drop_output_sections(raw, ("predictions",)).decode()

    assert "predictions_backup/old.csv" in kept
    assert "b/predictions/predictions.csv" not in kept


@pytest.mark.asyncio
async def test_declared_output_root_cannot_hide_changed_source(tmp_path) -> None:
    workspace_dir = tmp_path / "validate"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "solution/model.py"]],
                "outputs": {"predictions": "solution"},
            }
        ),
        encoding="utf-8",
    )
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    diff = GitDiff(
        ref=await store.put_bytes(
            _diff_section("solution/model.py", "+HIDDEN_SIZE = 128").encode()
        ),
        paths=("solution/model.py",),
    )
    reviewer_called = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewer_called
        reviewer_called = True
        return ValidationDiffReview(accepted=True, reason="looks safe")

    result = await review_validation_diff(
        workspace=GitWorkBranch(
            path=str(workspace_dir), branch="validate", base_commit="sota-a"
        ),
        diff=diff,
        explanation="retune the model",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False
    assert "model or training semantics" in result.reason
    assert reviewer_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["solution/model.py", "solution/features.py"])
async def test_validation_rejects_semantic_changes(tmp_path, path: str) -> None:
    store, (workspace, diff) = await _validation_branch(
        tmp_path, path, "HIDDEN_SIZE = 128"
    )
    reviewer_called = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewer_called
        reviewer_called = True
        return ValidationDiffReview(accepted=True, reason="looks safe")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="improve score",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False
    assert reviewer_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "content", "explanation"),
    [
        ("pyproject.toml", "[project]\ndependencies = ['pandas']", "add dependency"),
        ("solution/runtime.py", "DEVICE = 'cpu'", "repair unavailable device"),
        ("solution/runtime.py", "SEED = 7", "make runtime seed deterministic"),
        ("solution/config.py", "SEED = 7", "make runtime seed deterministic"),
        (
            "solution/io.py",
            "predictions.to_csv('predictions.csv')",
            "repair serialization",
        ),
    ],
)
async def test_validation_allows_dependency_path_device_seed_and_serialization_repairs(
    tmp_path, path: str, content: str, explanation: str
) -> None:
    store, (workspace, diff) = await _validation_branch(tmp_path, path, content)

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        return ValidationDiffReview(accepted=True, reason="runtime-only repair")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation=explanation,
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("solution/model.py", "LAYERS = 4"),
        ("solution/features.py", "FEATURES.append('age_bucket')"),
        ("solution/preprocess.py", "data = normalize(data)"),
        ("solution/config.py", "LEARNING_RATE = 0.01"),
        ("solution/options.py", "HIDDEN_SIZE = 128"),
        ("solution/train.py", "epochs = 100"),
    ],
)
async def test_validation_rejects_architecture_feature_preprocessing_parameter_and_training_changes(
    tmp_path, path: str, content: str
) -> None:
    store, (workspace, diff) = await _validation_branch(tmp_path, path, content)

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        return ValidationDiffReview(accepted=True, reason="accepted")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="improve final score",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False


@pytest.mark.asyncio
async def test_validation_rejects_final_label_access_before_llm_review(
    tmp_path,
) -> None:
    store, (workspace, diff) = await _validation_branch(
        tmp_path,
        "solution/runtime.py",
        "labels = read_csv('final_labels.csv')",
    )
    reviewer_called = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewer_called
        reviewer_called = True
        return ValidationDiffReview(accepted=True, reason="accepted")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="repair runtime",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False
    assert reviewer_called is False


@pytest.mark.asyncio
async def test_validation_requires_agent_explanation(tmp_path) -> None:
    store, (workspace, diff) = await _validation_branch(
        tmp_path, "solution/runtime.py", "SEED = 7"
    )
    reviewer_called = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewer_called
        reviewer_called = True
        return ValidationDiffReview(accepted=True, reason="accepted")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="   ",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False
    assert reviewer_called is False


@pytest.mark.asyncio
async def test_validation_requires_independent_acceptance(tmp_path) -> None:
    store, (workspace, diff) = await _validation_branch(
        tmp_path, "solution/runtime.py", "SEED = 7"
    )

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        return ValidationDiffReview(accepted=False, reason="changes training semantics")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="repair runtime seed",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False
    assert result.reason == "changes training semantics"


@pytest.mark.asyncio
async def test_validation_reviews_authoritative_diff_artifact_not_live_files(
    tmp_path,
) -> None:
    store, (workspace, diff) = await _validation_branch(
        tmp_path, "solution/runtime.py", "SEED = 7"
    )
    (Path(workspace.path) / "solution" / "runtime.py").write_text(
        "HIDDEN_SIZE = 1024", encoding="utf-8"
    )
    prompts: list[str] = []

    async def reviewer(prompt: str) -> ValidationDiffReview:
        prompts.append(prompt)
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="repair runtime seed",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is True
    assert "SEED = 7" in prompts[0]
    assert "HIDDEN_SIZE" not in prompts[0]


@pytest.mark.asyncio
async def test_validation_redacts_diff_with_shared_event_policy(tmp_path) -> None:
    secret = "sk-validation-secret-value"
    store, (workspace, diff) = await _validation_branch(
        tmp_path,
        "solution/runtime.py",
        f"ENDPOINT = 'https://example.invalid'  # {secret}",
    )
    prompts: list[str] = []

    async def reviewer(prompt: str) -> ValidationDiffReview:
        prompts.append(prompt)
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="repair runtime endpoint",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is True
    assert secret not in prompts[0]
    assert "[REDACTED]" in prompts[0]


@pytest.mark.asyncio
async def test_validation_rejects_binary_diff_without_llm_review(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "diff-artifacts")
    ref = await store.put_bytes(b"GIT binary patch\n\x00unsafe")
    workspace = GitWorkBranch(
        path=str(tmp_path / "validate"), branch="validate", base_commit="sota-a"
    )
    diff = GitDiff(ref=ref, paths=("weights.bin",))
    reviewer_called = False

    async def reviewer(_prompt: str) -> ValidationDiffReview:
        nonlocal reviewer_called
        reviewer_called = True
        return ValidationDiffReview(accepted=True, reason="accepted")

    result = await review_validation_diff(
        workspace=workspace,
        diff=diff,
        explanation="repair runtime",
        independent_review=reviewer,
        store=store,
    )

    assert result.accepted is False
    assert reviewer_called is False


class _StubExecution:
    def __init__(
        self, environment_root: Path, *, produce_predictions: bool = False
    ) -> None:
        self.environment_root = environment_root
        self.produce_predictions = produce_predictions
        self.timeouts: list[object] = []

    async def run(self, context, request):
        # ``ExecutionRuntime.run`` 现在收一个 CommandRequest（138c5b6）。旧签名下
        # 那个对象被位置绑到 ``command``，于是 timeout 永远读成 None。
        self.timeouts.append(request.timeout_s)
        if self.produce_predictions:
            out = Path(context.workspace_root) / "predictions" / "new.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("id,pred\n1,0\n", encoding="utf-8")

        class _Ok:
            ok = True
            stdout = ""
            stderr = ""
            exit_code = 0

        return _Ok()


class _StubGit:
    def __init__(self) -> None:
        self.restored: list[tuple[str, ...]] = []

    async def restore_paths(self, workspace, paths) -> None:
        del workspace
        self.restored.append(tuple(paths))


@pytest.mark.asyncio
async def test_execute_predictions_packs_predictions_directory(tmp_path) -> None:
    workdir = tmp_path / "validate"
    # If stale files exist before the run, the freshness guard must archive them.
    (workdir / "predictions").mkdir(parents=True)
    (workdir / "predictions" / "stale.csv").write_bytes(b"id,pred\n0,0\n")
    (workdir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "infer.py"]],
                "outputs": {"predictions": "predictions", "report": "REPORT.md"},
            }
        ),
        encoding="utf-8",
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = GitWorkBranch(
        path=str(workdir), branch="validate", base_commit="sota-a"
    )
    git = _StubGit()

    run = await _execute_predictions(
        execution=_StubExecution(workdir, produce_predictions=True),
        git=git,
        workspace=workspace,
        store=store,
        publish=None,
    )

    assert run.predictions_path == "predictions"
    packed = await load_directory(store, run.predictions_ref)
    assert list(packed) == ["new.csv"]
    # Stale file is archived outside the Git worktree, not packed.
    history = workdir.parent / "validate-output-history" / "validate"
    assert (history / "predictions" / "stale.csv").is_file()
    assert git.restored == [("predictions", "REPORT.md")]


@pytest.mark.asyncio
async def test_validation_rerun_gets_the_same_budget_search_gave_the_experiment(
    tmp_path,
) -> None:
    """VALIDATE 复跑此前不传 ``timeout_s``，于是拿的是 ``ExecutionRuntime.run`` 的
    120 秒默认值，而 SEARCH 跑同样的命令用的是 ``experiment_timeout_s``（默认一小时）。

    结果是：任何超过两分钟的实验——只要牵涉真训练或稍大的特征提取就都超过——
    过得了 SEARCH，却在 VALIDATE 因超时挂掉，看起来像候选自己坏了。
    """
    workdir = tmp_path / "validate"
    (workdir / "predictions").mkdir(parents=True)
    (workdir / "predictions" / "pred.csv").write_bytes(b"id,pred\n1,0\n")
    (workdir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "infer.py"], ["python", "score.py"]],
                "outputs": {"predictions": "predictions"},
            }
        ),
        encoding="utf-8",
    )
    execution = _StubExecution(workdir, produce_predictions=True)

    await _execute_predictions(
        execution=execution,
        git=_StubGit(),
        workspace=GitWorkBranch(
            path=str(workdir), branch="validate", base_commit="sota-a"
        ),
        store=LocalArtifactStore(tmp_path / "artifacts"),
        publish=None,
        timeout_s=5400,
    )

    assert execution.timeouts == [5400, 5400]


@pytest.mark.asyncio
async def test_execute_predictions_rejects_missing_directory(tmp_path) -> None:
    workdir = tmp_path / "validate"
    workdir.mkdir(parents=True)
    (workdir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [],
                "outputs": {"predictions": "predictions"},
            }
        ),
        encoding="utf-8",
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = GitWorkBranch(
        path=str(workdir), branch="validate", base_commit="sota-a"
    )
    git = _StubGit()

    with pytest.raises(ValueError, match="produced no new artifact"):
        await _execute_predictions(
            execution=_StubExecution(workdir),
            git=git,
            workspace=workspace,
            store=store,
            publish=None,
        )
    assert git.restored == [("predictions",)]

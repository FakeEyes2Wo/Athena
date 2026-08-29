import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.research.contracts import ValidationResult
from athena.research.script_runner import load_directory
from athena.research.supervisor.validation import (
    ValidationDiffReview,
    ValidationInput,
    _execute_predictions,
    recovery_action,
    review_validation_diff,
    validation_key,
)


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
    def __init__(self, environment_root: Path) -> None:
        self.environment_root = environment_root
        self.timeouts: list[object] = []

    async def run(self, context, command=None, *, argv=None, **kwargs):
        del context, command, argv
        self.timeouts.append(kwargs.get("timeout_s"))

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
    (workdir / "predictions" / "nested").mkdir(parents=True)
    (workdir / "predictions" / "pred.csv").write_bytes(b"id,pred\n1,0\n")
    (workdir / "predictions" / "nested" / "mask.bin").write_bytes(b"\x00\x01\x02")
    (workdir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [],
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

    ref, rel_path = await _execute_predictions(
        execution=_StubExecution(workdir),
        git=git,
        workspace=workspace,
        store=store,
        publish=None,
    )

    assert rel_path == "predictions"
    assert await load_directory(store, ref) == {
        "pred.csv": b"id,pred\n1,0\n",
        "nested/mask.bin": b"\x00\x01\x02",
    }
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
    execution = _StubExecution(workdir)

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

    with pytest.raises(ValueError, match="predictions output is missing"):
        await _execute_predictions(
            execution=_StubExecution(workdir),
            git=git,
            workspace=workspace,
            store=store,
            publish=None,
        )
    assert git.restored == [("predictions",)]

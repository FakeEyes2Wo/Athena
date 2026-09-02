import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from athena.agents.task_agents import register_validate_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.execution.runtime import CommandResult, ExecutionRuntime
from athena.research.contracts import CandidateEvaluation, EvaluatorDescriptor
from athena.research.supervisor import validation as validation_module
from athena.research.supervisor.validation import (
    ValidationDeps,
    ValidationDiffReview,
    ValidationInput,
    ValidationOptions,
    ValidationRunFailed,
    run_validation_plan,
    validation_key,
)


@pytest.fixture
def tmp_path():
    """Worktree-scoped temp dir; the sandbox blocks the default pytest temp root."""
    base = Path(__file__).resolve().parents[3] / ".pytest-tmp-validate-contract"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"tmp-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


class _RepairProvider:
    model_name = "validate-contract-test"

    def __init__(self) -> None:
        self.calls = 0
        self.feedback_seen = ""

    async def stream(self, _config, _tools, messages, _cancel, **_kwargs):
        self.calls += 1
        self.feedback_seen = "\n".join(
            str(getattr(part, "content", ""))
            for message in messages
            for part in getattr(message, "parts", ())
        )
        if self.calls in (1, 3):
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"write-{self.calls}",
                    "name": "write_file",
                    "arguments": {
                        "path": "solution/runtime.py",
                        "content": "SEED = 7\n",
                    },
                },
            )
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"manifest-{self.calls}",
                    "name": "write_file",
                    "arguments": {
                        "path": "experiment.json",
                        "content": json.dumps(
                            {
                                "version": 1,
                                "commands": [["python", "predict.py"]],
                                "outputs": {"predictions": "predictions"},
                            }
                        ),
                    },
                },
            )
        else:
            payload = '{"explanation":"Repair runtime seed and serialization only."}'
            yield StreamEvent(
                kind="text_delta", data={"delta": payload, "accumulated": payload}
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _Execution(ExecutionRuntime):
    def __init__(self, workdir: Path, store: LocalArtifactStore) -> None:
        super().__init__(project_root=workdir, store=store)
        self.calls = 0
        self.mutate_source_once = False
        # 让前 N 次运行以候选脚本的方式失败（非零退出 + traceback），
        # 用于验证修复循环会把失败正文回灌给 validate Agent。
        self.fail_runs = 0
        self.failure_stderr = ""

    async def run(self, context, request):
        self.calls += 1
        if self.fail_runs > 0:
            self.fail_runs -= 1
            return CommandResult(
                ok=False,
                stdout="",
                stderr=self.failure_stderr,
                exit_code=1,
            )
        result = await super().run(context, request)
        if self.mutate_source_once:
            self.mutate_source_once = False
            (Path(context.workspace_root) / "solution" / "runtime.py").write_text(
                "SEED = 999\n", encoding="utf-8"
            )
        return result


class _Evaluator:
    def __init__(self) -> None:
        self.calls = 0
        self.directions: list[str] = []
        self.evaluator_dirs: list[Path] = []

    async def score(
        self,
        *,
        evaluator_dir: Path,
        predictions: dict[str, bytes],
        candidate_id: str,
        direction: str,
        predictions_root: str = "predictions.csv",
    ):
        del predictions
        self.calls += 1
        self.directions.append(direction)
        self.evaluator_dirs.append(evaluator_dir)
        return CandidateEvaluation(
            candidate_id=candidate_id, test_score=0.79, direction=direction
        )


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.repo = tmp_path / "repo"
        self.worktrees = tmp_path / "worktrees"
        self.store = LocalArtifactStore(tmp_path / "artifacts")
        self.provider = _RepairProvider()
        self.evaluator = _Evaluator()
        self.review_calls = 0
        self.review_prompts: list[str] = []
        self.reject_once = False
        self.review_execution_counts: list[int] = []
        self.branch = None
        self.git = None
        self.agents = None
        self.execution = None
        self.input = None
        self.result_ref = None
        self.checkpoints: list[str] = []
        self.restore_calls: list[tuple[str, ...]] = []

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    async def start(self) -> None:
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Athena Test")
        self._git("config", "user.email", "athena@example.invalid")
        (self.repo / "solution").mkdir()
        (self.repo / "solution" / "runtime.py").write_text(
            "SEED = None\n", encoding="utf-8"
        )
        (self.repo / "predict.py").write_text(
            "from pathlib import Path\n"
            "Path('predictions').mkdir(parents=True, exist_ok=True)\n"
            "Path('predictions/pred.csv').write_text('id,pred\\n1,0\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        (self.repo / "predictions").mkdir()
        (self.repo / "predictions" / "pred.csv").write_text(
            "id,pred\nreviewed,1\n", encoding="utf-8"
        )
        self._git("add", "-A")
        self._git("commit", "--no-gpg-sign", "-m", "sota")
        sota = self._git("rev-parse", "HEAD")

        async def write_diff(content: bytes) -> str:
            return await self.store.put_bytes(content)

        self.git = LocalGitWorkspace(self.repo, self.worktrees, write_diff)
        self.branch = await self.git.create(sota, "athena/validate")
        restore_paths = self.git.restore_paths

        async def record_restore(workspace, paths):
            self.restore_calls.append(paths)
            await restore_paths(workspace, paths)

        self.git.restore_paths = record_restore
        registry = AgentTypeRegistry()
        runtime = ExecutionRuntime(
            project_root=Path(self.branch.path), store=self.store
        )
        register_validate_agent(
            registry,
            provider=self.provider,
            artifacts=self.store,
            workspace=Path(self.branch.path),
            runtime=runtime,
        )
        self.agents = AgentRuntime(
            type_registry=registry,
            project_root=self.tmp_path,
            rollout_dir=self.tmp_path / ".athena" / "logs" / "agents",
        )
        self.agents.start()
        self.execution = _Execution(Path(self.branch.path), self.store)
        final_evaluator_dir = self.tmp_path / "final_eval"
        final_evaluator_dir.mkdir(parents=True, exist_ok=True)
        readme = "# Evaluator Freeze Marker\n\nDo not edit.\n"
        (final_evaluator_dir / "README.md").write_text(readme, encoding="utf-8")
        readme_ref = await self.store.put_text(readme)
        final_evaluator_ref = await self.store.put_text(
            EvaluatorDescriptor(
                dir_path=str(final_evaluator_dir),
                readme_ref=readme_ref,
                entrypoint="eval.py",
            ).model_dump_json()
        )
        self.input = ValidationInput(
            sota_commit=sota,
            reference_metric=0.82,
            direction="maximize",
            final_evaluator_ref=final_evaluator_ref,
            validation_key=validation_key(sota, 0.82, "maximize", final_evaluator_ref),
        )

    async def reviewer(self, prompt: str) -> ValidationDiffReview:
        self.review_calls += 1
        self.review_execution_counts.append(self.execution.calls)
        self.review_prompts.append(prompt)
        if self.reject_once and self.review_calls == 1:
            return ValidationDiffReview(
                accepted=False, reason="clarify runtime-only repair"
            )
        return ValidationDiffReview(accepted=True, reason="runtime-only")

    async def checkpoint(self, result_ref: str) -> None:
        self.result_ref = result_ref
        self.checkpoints.append(result_ref)

    async def run(self):
        deps = ValidationDeps(
            agents=self.agents,
            git=self.git,
            workspace=self.branch,
            execution=self.execution,
            evaluator=self.evaluator,
            store=self.store,
            independent_review=self.reviewer,
            checkpoint=self.checkpoint,
        )
        return await run_validation_plan(
            input=self.input,
            deps=deps,
            options=ValidationOptions(),
            result_ref=self.result_ref,
        )

    async def close(self) -> None:
        await self.agents.aclose()
        await self.git.remove(self.branch, delete_branch=True, force=True)


@pytest.mark.asyncio
async def test_validate_uses_stable_validate_agent_plan_and_workspace(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    result = await harness.run()

    assert result.sota_commit == harness.input.sota_commit
    assert result.validation_commit != result.sota_commit
    assert harness.review_execution_counts == [0]
    assert "predictions" in harness.review_prompts[-1]
    assert harness.restore_calls == [("predictions",)]
    assert (Path(harness.branch.path) / "predictions" / "pred.csv").read_text(
        encoding="utf-8"
    ) == "id,pred\nreviewed,1\n"
    assert len(harness.checkpoints) == 3
    assert not (Path(harness.branch.path).parent / ".athena-validation").exists()
    assert (tmp_path / ".athena" / "logs" / "agents" / "validate.jsonl").is_file()
    await harness.close()


@pytest.mark.asyncio
async def test_rejected_diff_returns_feedback_to_same_agent(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    harness.reject_once = True

    await harness.run()

    assert harness.review_calls == 2
    assert harness.review_execution_counts == [0, 0]
    assert "clarify runtime-only repair" in harness.provider.feedback_seen
    await harness.close()


@pytest.mark.asyncio
async def test_command_source_mutation_is_rejected_before_score(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    harness.execution.mutate_source_once = True

    await harness.run()

    assert harness.execution.calls == 2
    assert harness.evaluator.calls == 1
    assert harness.review_execution_counts == [0, 1]
    assert (
        "workspace changed after independent review" in harness.provider.feedback_seen
    )
    await harness.close()


@pytest.mark.asyncio
async def test_missing_review_evidence_restarts_review_before_commit(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    first = await harness.run()
    review_calls = harness.review_calls
    payload = json.loads(await harness.store.get_text(harness.result_ref))
    payload["final_test_score"] = None
    payload["evidence_ref"] = None
    payload["validation_commit"] = None
    payload["status"] = "FAILED"
    harness.result_ref = await harness.store.put_text(json.dumps(payload))

    resumed = await harness.run()

    assert first.predictions_ref == resumed.predictions_ref
    assert harness.review_calls == review_calls + 1
    assert harness.review_execution_counts[-1] == 1
    assert harness.execution.calls == 2
    assert resumed.generalization_gap == pytest.approx(0.03)
    assert resumed.generalization_warning is True
    await harness.close()


@pytest.mark.asyncio
async def test_partial_or_invalid_output_reruns_under_same_key(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    harness.result_ref = await harness.store.put_text("not-json")

    result = await harness.run()

    assert result.result_id == harness.input.validation_key
    assert harness.execution.calls == 1
    await harness.close()


@pytest.mark.asyncio
async def test_complete_result_for_another_sota_reruns_current_validation(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    harness.result_ref = await harness.store.put_text(
        json.dumps(
            {
                "result_id": harness.input.validation_key,
                "status": "COMPLETED",
                "test_score": 0.9,
                "final_test_score": 0.9,
                "sota_commit": "foreign-sota",
                "validation_commit": "foreign-commit",
                "predictions_ref": "foreign-predictions",
                "evidence_ref": "foreign-evidence",
            }
        )
    )

    result = await harness.run()

    assert result.result_id == harness.input.validation_key
    assert result.sota_commit == harness.input.sota_commit
    assert harness.execution.calls == 1
    await harness.close()


@pytest.mark.asyncio
async def test_completed_result_from_another_validation_key_is_not_reused(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    harness.result_ref = await harness.store.put_text(
        json.dumps(
            {
                "result_id": "another-validation-key",
                "status": "COMPLETED",
                "test_score": 0.82,
                "final_test_score": 0.79,
                "sota_commit": harness.input.sota_commit,
                "validation_commit": "foreign-commit",
                "predictions_ref": "foreign-predictions",
                "evidence_ref": "foreign-evidence",
            }
        )
    )

    result = await harness.run()

    assert result.result_id == harness.input.validation_key
    assert result.validation_commit != "foreign-commit"
    assert harness.execution.calls == 1
    await harness.close()


@pytest.mark.asyncio
async def test_crash_after_score_does_not_score_twice(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    real_commit = harness.git.commit
    commit_calls = 0

    async def crash_once(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise RuntimeError("crash after score")
        return await real_commit(*args, **kwargs)

    harness.git.commit = crash_once
    with pytest.raises(RuntimeError, match="crash after score"):
        await harness.run()
    score_calls = harness.evaluator.calls

    async def write_diff(content: bytes) -> str:
        return await harness.store.put_bytes(content)

    harness.git = LocalGitWorkspace(harness.repo, harness.worktrees, write_diff)
    harness.branch = await harness.git.create(
        harness.input.sota_commit, "athena/validate"
    )

    result = await harness.run()

    assert harness.evaluator.calls == score_calls == 1
    assert result.validation_commit != result.sota_commit
    await harness.close()


@pytest.mark.asyncio
async def test_complete_result_commits_without_execution_or_scoring(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    await harness.run()
    execution_calls = harness.execution.calls
    evaluator_calls = harness.evaluator.calls
    diff_calls = 0
    commit_calls = 0
    real_diff = harness.git.diff
    real_commit = harness.git.commit

    async def count_diff(*args, **kwargs):
        nonlocal diff_calls
        diff_calls += 1
        return await real_diff(*args, **kwargs)

    async def count_commit(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        return await real_commit(*args, **kwargs)

    harness.git.diff = count_diff
    harness.git.commit = count_commit

    await harness.run()

    assert harness.execution.calls == execution_calls
    assert harness.evaluator.calls == evaluator_calls
    assert diff_calls == 0
    assert commit_calls == 0
    await harness.close()


_REAL_CRASH_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "solution/train_model.py", line 255, in main\n'
    "    pr_auc_search = average_precision_score(y_search_true, y_pred_search)\n"
    "ValueError: Found input variables with inconsistent numbers of "
    "samples: [169725, 169965]"
)


@pytest.mark.asyncio
async def test_run_failure_is_returned_to_the_validate_agent(tmp_path) -> None:
    """A crashing frozen command must reach the Agent hired to repair crashes.

    Until this passed, the first failure propagated straight out of the repair
    loop: the Agent was created with "repair runtime-only failures" and then
    never shown one. On 2026-08-31 that discarded a five-hour run whose eight
    SEARCH experiments had all completed.
    """
    harness = _Harness(tmp_path)
    await harness.start()
    harness.execution.fail_runs = 1
    harness.execution.failure_stderr = _REAL_CRASH_STDERR

    result = await harness.run()

    assert result.final_test_score == pytest.approx(0.79)
    assert harness.execution.calls == 2
    assert harness.review_calls == 2
    assert "inconsistent numbers of samples" in harness.provider.feedback_seen
    assert "The frozen SOTA command failed when re-run" in harness.provider.feedback_seen
    # 失败后仍要还原声明产物，否则第二次运行会在陈旧目录上打分。
    assert harness.restore_calls == [("predictions",), ("predictions",)]
    await harness.close()


@pytest.mark.asyncio
async def test_repeated_run_failure_names_the_failure_without_exhausting_budget(
    tmp_path, monkeypatch
) -> None:
    """An unchanged failed repair must carry the cause and stop early.

    Waiting for generic budget exhaustion spends more Agent turns without adding
    evidence; the no-progress guard names both the stage and repeated traceback.
    """
    monkeypatch.setattr(validation_module, "_MAX_VALIDATION_REPAIR_ATTEMPTS", 2)
    harness = _Harness(tmp_path)
    await harness.start()
    harness.execution.fail_runs = 99
    harness.execution.failure_stderr = _REAL_CRASH_STDERR

    with pytest.raises(RuntimeError) as excinfo:
        await harness.run()

    assert "validation repair made no progress" in str(excinfo.value)
    assert "stage=execution" in str(excinfo.value)
    assert "inconsistent numbers of samples" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValidationRunFailed)
    assert harness.execution.calls == 2
    await harness.close()


@pytest.mark.asyncio
async def test_validation_never_changes_research_tree_sota(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    sota_before = harness.input.sota_commit

    result = await harness.run()

    assert harness.input.sota_commit == sota_before
    assert result.sota_commit == sota_before
    assert result.validation_commit != sota_before
    await harness.close()


@pytest.mark.asyncio
async def test_normal_validation_computes_gap_from_frozen_sota_metric(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()

    result = await harness.run()

    assert result.test_score == pytest.approx(0.82)
    assert result.final_test_score == pytest.approx(0.79)
    assert result.generalization_gap == pytest.approx(0.03)
    assert result.generalization_warning is True
    assert len(harness.evaluator.evaluator_dirs) == 1
    assert (harness.evaluator.evaluator_dirs[0] / "README.md").is_file()
    await harness.close()


@pytest.mark.asyncio
async def test_minimize_validation_uses_minimize_for_score_and_gap(tmp_path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    harness.input = harness.input.model_copy(
        update={
            "reference_metric": 0.76,
            "direction": "minimize",
            "validation_key": validation_key(
                harness.input.sota_commit,
                0.76,
                "minimize",
                harness.input.final_evaluator_ref,
            ),
        }
    )

    result = await harness.run()

    assert harness.evaluator.directions == ["minimize"]
    assert result.test_score == pytest.approx(0.76)
    assert result.final_test_score == pytest.approx(0.79)
    assert result.generalization_gap == pytest.approx(0.03)
    assert result.generalization_warning is True
    await harness.close()

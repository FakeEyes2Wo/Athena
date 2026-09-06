"""Deterministic PREPARE phase boundary tests."""

import asyncio
import csv
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.execution.runtime import CommandRequest, CommandResult
from athena.research.contracts import CandidateEvaluation, EvaluatorDescriptor
from athena.research.prepare.baseline_research import BaselineResearchError
from athena.research.supervisor import prepare
from athena.research.supervisor.evaluator_plan import (
    _validate_frozen_evaluator,
    run_evaluator_plan,
)
from athena.research.supervisor.prepare import run_prepare_plan


@pytest.mark.asyncio
async def test_prepare_agent_reap_is_bounded(monkeypatch) -> None:
    """Cleanup returns even when reap ignores cancellation after its timeout."""
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def ignores_cancellation(_agent_id: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Timeout cancellation is intentionally ignored to model the real hang.
            cancelled.set()
            await release.wait()
        finally:
            finished.set()

    monkeypatch.setattr(prepare, "_REAP_TIMEOUT_SECONDS", 0.01)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(
        prepare._reap_agent(SimpleNamespace(reap=ignores_cancellation), "prepare"),
        timeout=0.2,
    )
    assert asyncio.get_running_loop().time() - started < 0.1
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=0.1)


class _AgentRuntime:
    def __init__(self, store: LocalArtifactStore, agent_id: str = "prepare") -> None:
        self.store = store
        self._agent_id = agent_id
        self.created: list[str] = []
        self.feedback: list[str] = []
        self._responses: dict[str, str] = {}
        self._next = 0

    async def _response(self, run_id: str) -> None:
        decision_ref = await self.store.put_text(
            json.dumps({"decision": "submit", "reason": "ready"})
        )
        self._responses[run_id] = json.dumps({"result_ref": decision_ref})

    async def create_root(self, agent_type, task, *, name, agent_id):
        del task, name
        self.created.append(agent_type)
        self._next += 1
        run_id = f"run-{self._next}"
        await self._response(run_id)
        return agent_id, run_id

    async def followup(self, agent_id, task):
        assert agent_id == self._agent_id
        self.feedback.append(task["content"])
        self._next += 1
        run_id = f"run-{self._next}"
        await self._response(run_id)
        return run_id

    async def wait_run(self, run_id, *, timeout=None):
        del timeout
        return type(
            "Summary",
            (),
            {
                "status": type("Status", (), {"value": "completed"})(),
                "response_ref": self._responses[run_id],
                "error": None,
            },
        )()

    async def run_events(self, _run_id, after_sequence=0):
        yield SimpleNamespace(
            kind="response_completed",
            event_ref="event-ref",
            data={},
            sequence=after_sequence + 1,
        )

    async def reap(self, agent_id: str) -> None:
        del agent_id


class _InvalidFirstDecisionRuntime(_AgentRuntime):
    async def _response(self, run_id: str) -> None:
        if self._next == 1:
            decision_ref = await self.store.put_text("not-json")
            self._responses[run_id] = json.dumps({"result_ref": decision_ref})
            return
        await super()._response(run_id)


class _Scripts:
    """Score synthetic CSV probes without starting a subprocess."""

    async def run_dir(self, root, *, request, extra_files, output_schema):
        del request, output_schema
        labels_path = root / "labels.csv"
        if not labels_path.is_file():
            labels_path = next((root / "labels").rglob("*.csv"))
        with labels_path.open(encoding="utf-8-sig", newline="") as handle:
            labels = list(csv.DictReader(handle))
        predictions = list(
            csv.DictReader(next(iter(extra_files.values())).decode().splitlines())
        )
        id_column, prediction_column = predictions[0].keys()
        target_column = next(name for name in labels[0] if name != id_column)
        truth = {row[id_column]: row[target_column] for row in labels}
        score = sum(
            truth[row[id_column]] == row[prediction_column] for row in predictions
        ) / len(predictions)
        return SimpleNamespace(outputs={"primary": score})


class _Execution:
    def __init__(self, root: Path) -> None:
        self.project_root = root
        self.environment_root = root

    def ensure_environment(self) -> None:
        pass

    async def run(self, context, request: CommandRequest):
        del context
        workdir = Path(request.workdir)
        history = workdir.parent / f"{workdir.name}-output-history"
        versions = sorted(path for path in history.iterdir() if path.is_dir())
        manifest = json.loads((workdir / "experiment.json").read_text("utf-8"))
        for relative in manifest["outputs"].values():
            source = versions[-1] / relative
            target = workdir / relative
            if not source.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
                continue
            if target.is_dir():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return CommandResult(ok=True, stdout="", stderr="", exit_code=0)

    async def collect_outputs(self, _subdirs: tuple[str, ...]) -> None:
        return None


def _evaluator_runtime(tmp_path, store, agents, scripts=None):
    async def publish_agent_event(_plan_id, _kind, _ref, _data):
        return None

    return SimpleNamespace(
        agents=agents,
        scripts=scripts or _Scripts(),
        store=store,
        execution=_Execution(tmp_path),
        events=SimpleNamespace(project_agent_event=publish_agent_event),
    )


class _Evaluator:
    async def score(self, **kwargs):
        del kwargs
        return CandidateEvaluation(
            candidate_id="prepare", test_score=0.75, direction="maximize"
        )


async def _passing_baseline_guard() -> None:
    return None


class _FailingEvaluator:
    async def score(self, **kwargs):
        del kwargs
        raise ValueError("row ids do not align")


class _Git:
    def __init__(self, *, missing_commit: bool = False) -> None:
        self.missing_commit = missing_commit

    async def diff(self, workspace):
        del workspace
        return GitDiff(ref="sha256:" + "6" * 64, paths=("model.py",))

    async def commit(self, workspace, approved_diff, message):
        del workspace, approved_diff, message
        return None if self.missing_commit else "commit-baseline"


class _Store:
    def __init__(self, root: Path, *, missing_evidence: bool = False) -> None:
        self.inner = LocalArtifactStore(root)
        self.missing_evidence = missing_evidence

    async def put_bytes(self, data):
        return await self.inner.put_bytes(data)

    async def get_bytes(self, ref):
        return await self.inner.get_bytes(ref)

    async def put_text(self, text):
        if self.missing_evidence and '"metric"' in text and '"commit"' in text:
            return None
        return await self.inner.put_text(text)

    async def get_text(self, ref):
        return await self.inner.get_text(ref)


def _write_eda_workspace(workspace_path: Path, *, missing: str | None) -> None:
    """Write the experiment-side baseline artifacts into the EDA workspace."""
    workspace_path.mkdir(parents=True, exist_ok=True)
    (workspace_path / "model.py").write_text("pass\n", encoding="utf-8")
    (workspace_path / "outputs" / "predictions").mkdir(parents=True)
    if missing != "predictions":
        (workspace_path / "outputs" / "predictions" / "predictions.csv").write_text(
            "id,prediction\n1,0\n", encoding="utf-8"
        )
    if missing != "report":
        (workspace_path / "outputs" / "report.md").write_text(
            "# Baseline\n", encoding="utf-8"
        )
    (workspace_path / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "model.py"]],
                "outputs": {
                    "predictions": "outputs/predictions",
                    "report": "outputs/report.md",
                },
            }
        ),
        encoding="utf-8",
    )


async def _frozen_evaluator_ref(store, dir_path: Path) -> str:
    dir_path.mkdir(parents=True, exist_ok=True)
    readme = "# Evaluator Freeze Marker\n\nDo not edit.\n"
    (dir_path / "README.md").write_text(readme, encoding="utf-8")
    readme_ref = await store.put_text(readme)
    descriptor = EvaluatorDescriptor(
        dir_path=str(dir_path),
        readme_ref=readme_ref,
        entrypoint="evaluate.py",
    )
    return await store.put_text(descriptor.model_dump_json())


class _RecordingEvaluator(_Evaluator):
    def __init__(self) -> None:
        self.calls = 0

    async def score(self, **kwargs):
        self.calls += 1
        return await super().score(**kwargs)


class _MutatingAgentRuntime(_AgentRuntime):
    def __init__(self, store: LocalArtifactStore, marker: Path) -> None:
        super().__init__(store)
        self.marker = marker

    async def wait_run(self, run_id, *, timeout=None):
        summary = await super().wait_run(run_id, timeout=timeout)
        self.marker.write_text("tampered", encoding="utf-8")
        return summary


class _MutatingExecution(_Execution):
    def __init__(self, root: Path, marker: Path) -> None:
        super().__init__(root)
        self.marker = marker

    async def run(self, context, request: CommandRequest):
        result = await super().run(context, request)
        self.marker.write_text("tampered", encoding="utf-8")
        return result


def _matching_guard(marker: Path):
    async def assert_baseline() -> None:
        if marker.read_text(encoding="utf-8") != "sealed":
            raise BaselineResearchError("baseline evidence was tampered")

    return assert_baseline


@pytest.mark.asyncio
async def test_agent_turn_mutation_is_terminal_before_plan_or_evaluator(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "eda"
    _write_eda_workspace(workspace_path, missing=None)
    marker = workspace_path / "baseline.marker"
    marker.write_text("sealed", encoding="utf-8")
    store = _Store(tmp_path / "artifacts")
    agents = _MutatingAgentRuntime(store.inner, marker)
    evaluator = _RecordingEvaluator()
    evaluator_ref = await _frozen_evaluator_ref(store, tmp_path / "evaluator")
    tree_ref = await store.put_text('{"experiments": []}')

    with pytest.raises(BaselineResearchError, match="tampered"):
        await run_prepare_plan(
            agents=agents,
            evaluator=evaluator,
            git=_Git(),
            workspace=GitWorkBranch(
                path=str(workspace_path), branch="prepare", base_commit="base"
            ),
            execution=_Execution(tmp_path),
            store=store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task="build baseline",
            max_turns=2,
            assert_baseline=_matching_guard(marker),
        )

    assert evaluator.calls == 0
    assert agents.feedback == []


@pytest.mark.asyncio
async def test_execution_mutation_is_terminal_immediately_before_evaluator(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "eda"
    _write_eda_workspace(workspace_path, missing=None)
    marker = workspace_path / "baseline.marker"
    marker.write_text("sealed", encoding="utf-8")
    store = _Store(tmp_path / "artifacts")
    agents = _AgentRuntime(store.inner)
    evaluator = _RecordingEvaluator()
    evaluator_ref = await _frozen_evaluator_ref(store, tmp_path / "evaluator")
    tree_ref = await store.put_text('{"experiments": []}')

    with pytest.raises(BaselineResearchError, match="tampered"):
        await run_prepare_plan(
            agents=agents,
            evaluator=evaluator,
            git=_Git(),
            workspace=GitWorkBranch(
                path=str(workspace_path), branch="prepare", base_commit="base"
            ),
            execution=_MutatingExecution(tmp_path, marker),
            store=store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task="build baseline",
            max_turns=2,
            assert_baseline=_matching_guard(marker),
        )

    assert evaluator.calls == 0
    assert agents.feedback == []


@pytest.mark.asyncio
async def test_plain_guard_error_before_evaluator_is_terminal_not_feedback(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "eda"
    _write_eda_workspace(workspace_path, missing=None)
    store = _Store(tmp_path / "artifacts")
    agents = _AgentRuntime(store.inner)
    evaluator = _RecordingEvaluator()
    evaluator_ref = await _frozen_evaluator_ref(store, tmp_path / "evaluator")
    tree_ref = await store.put_text('{"experiments": []}')
    checks = 0

    async def fail_at_scoring_boundary() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError("baseline guard unavailable")

    with pytest.raises(RuntimeError, match="baseline evidence guard failed"):
        await run_prepare_plan(
            agents=agents,
            evaluator=evaluator,
            git=_Git(),
            workspace=GitWorkBranch(
                path=str(workspace_path), branch="prepare", base_commit="base"
            ),
            execution=_Execution(tmp_path),
            store=store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task="build baseline",
            max_turns=2,
            assert_baseline=fail_at_scoring_boundary,
        )

    assert checks == 2
    assert evaluator.calls == 0
    assert agents.feedback == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing", "expected_error"),
    [
        (
            "report",
            "report output produced no new artifact",
        ),
        (
            "predictions",
            "predictions output produced no new artifact",
        ),
        ("evidence", "trusted evidence is missing"),
        ("commit", "trusted commit is missing"),
    ],
)
async def test_prepare_result_requires_every_trusted_artifact(
    tmp_path: Path, missing: str, expected_error: str
) -> None:
    workspace_path = tmp_path / "eda"
    _write_eda_workspace(workspace_path, missing=missing)
    store = _Store(tmp_path / "artifacts", missing_evidence=missing == "evidence")
    agents = _AgentRuntime(store.inner)
    evaluator_ref = await _frozen_evaluator_ref(store, tmp_path / "evaluator")
    tree_ref = await store.put_text('{"experiments": []}')

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_prepare_plan(
            agents=agents,
            evaluator=_Evaluator(),
            git=_Git(missing_commit=missing == "commit"),
            workspace=GitWorkBranch(
                path=str(workspace_path), branch="prepare", base_commit="base"
            ),
            execution=_Execution(tmp_path),
            store=store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task="build baseline",
            max_turns=2,
            assert_baseline=_passing_baseline_guard,
        )

    assert agents.created == ["prepare"]
    assert len(agents.feedback) == 1
    assert agents.feedback[0].startswith(expected_error)


@pytest.mark.asyncio
async def test_prepare_returns_result_on_trusted_baseline(tmp_path: Path) -> None:
    workspace_path = tmp_path / "eda"
    _write_eda_workspace(workspace_path, missing=None)
    store = _Store(tmp_path / "artifacts")
    agents = _AgentRuntime(store.inner)
    evaluator_ref = await _frozen_evaluator_ref(store, tmp_path / "evaluator")
    tree_ref = await store.put_text('{"experiments": []}')

    result = await run_prepare_plan(
        agents=agents,
        evaluator=_Evaluator(),
        git=_Git(),
        workspace=GitWorkBranch(
            path=str(workspace_path), branch="prepare", base_commit="base"
        ),
        execution=_Execution(tmp_path),
        store=store,
        evaluator_ref=evaluator_ref,
        tree_ref=tree_ref,
        task="build baseline",
        max_turns=2,
        assert_baseline=_passing_baseline_guard,
    )

    assert result.metric == 0.75
    assert result.evaluator_ref == evaluator_ref
    assert result.commit == "commit-baseline"
    assert agents.created == ["prepare"]
    assert agents.feedback == []


@pytest.mark.asyncio
async def test_prepare_scoring_failure_retries_without_agent_repair(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "eda"
    _write_eda_workspace(workspace_path, missing=None)
    store = _Store(tmp_path / "artifacts")
    agents = _AgentRuntime(store.inner)
    evaluator_ref = await _frozen_evaluator_ref(store, tmp_path / "evaluator")
    tree_ref = await store.put_text('{"experiments": []}')

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_prepare_plan(
            agents=agents,
            evaluator=_FailingEvaluator(),
            git=_Git(),
            workspace=GitWorkBranch(
                path=str(workspace_path), branch="prepare", base_commit="base"
            ),
            execution=_Execution(tmp_path),
            store=store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task="build baseline",
            max_turns=2,
            assert_baseline=_passing_baseline_guard,
        )

    assert agents.created == ["prepare"]
    assert agents.feedback == ["row ids do not align"]


def _evaluator_draft(root: Path, labels_csv: str) -> None:
    """最小可冻结 evaluator 草稿，labels.csv 内容由用例给定。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "labels.csv").write_text(labels_csv, encoding="utf-8")
    (root / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (root / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )
    (root / "HANDOFF.md").write_text(
        "prediction column: prediction\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_a_bundled_prediction_file_is_rejected_before_the_probes(
    tmp_path: Path,
) -> None:
    """A stale copy beside the metric code shadows the file the platform writes.

    On 2026-09-06 an evaluator kept scoring its own sample predictions, so the
    sensitivity probe saw one constant score and rejected every submit until the
    turn budget ran out.
    """
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "__athena_row_id,label\n0,0\n1,1\n")
    (evaluator_dir / "metric.json").write_text(
        json.dumps(
            {
                "eval_script": "evaluate.py",
                "prediction_file": "predictions__task.csv",
            }
        ),
        encoding="utf-8",
    )
    (evaluator_dir / "predictions__task.csv").write_text(
        "__athena_row_id,prediction\n0,0\n1,1\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="must not contain a prediction file"):
        await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_labels_without_row_id_are_rejected_by_validation(tmp_path: Path) -> None:
    """单列 labels.csv 仍必须在 README 冻结前被结构性检查拦下。"""
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "label\n0\n1\n")
    with pytest.raises(ValueError, match="__athena_row_id"):
        await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_labels_with_wrong_row_id_are_rejected_by_validation(
    tmp_path: Path,
) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "id,label\n0,0\n1,1\n")
    with pytest.raises(ValueError, match="__athena_row_id"):
        await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_duplicate_row_ids_are_rejected_by_validation(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(
        evaluator_dir,
        "__athena_row_id,label\n0,0\n0,1\n",
    )
    with pytest.raises(ValueError, match="duplicate '__athena_row_id'"):
        await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_valid_tabular_labels_pass_validation(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "__athena_row_id,label\n0,0\n1,1\n")
    await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_labels_directory_and_handoff_are_accepted(tmp_path: Path) -> None:
    """Labels may be a ``labels/`` directory; HANDOFF.md stays next to evaluator."""
    evaluator_dir = tmp_path / "evaluator"
    (evaluator_dir / "labels").mkdir(parents=True)
    (evaluator_dir / "labels" / "truth.csv").write_text(
        "__athena_row_id,label\n1,0\n2,1\n", encoding="utf-8"
    )
    (evaluator_dir / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator_dir / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "HANDOFF.md").write_text(
        "# Handoff\n\nprediction column: prediction\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )
    await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_labels_directory_without_row_id_is_rejected(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    (evaluator_dir / "labels").mkdir(parents=True)
    (evaluator_dir / "labels" / "truth.csv").write_text(
        "id,label\n1,0\n", encoding="utf-8"
    )
    (evaluator_dir / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator_dir / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="__athena_row_id"):
        await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_eval_script_directory_labels_are_located_correctly(
    tmp_path: Path,
) -> None:
    """``eval_script`` may name a directory; labels are found next to evaluate.py."""
    evaluator_dir = tmp_path / "evaluator"
    inner = evaluator_dir / "evaluator"
    inner.mkdir(parents=True)
    (inner / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (inner / "labels.csv").write_text(
        "__athena_row_id,label\n1,0\n2,1\n", encoding="utf-8"
    )
    (inner / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (inner / "HANDOFF.md").write_text(
        "prediction column: prediction\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluator"}), encoding="utf-8"
    )
    await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_custom_format_skips_csv_probes(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    evaluator_dir.mkdir(parents=True)
    (evaluator_dir / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator_dir / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py", "prediction_format": "custom"}),
        encoding="utf-8",
    )
    # No CSV labels required: custom formats are validated by the agent's own
    # format-aware probes instead of the platform CSV property tests.
    await _validate_frozen_evaluator(root=evaluator_dir, scripts=_Scripts())


@pytest.mark.asyncio
async def test_evaluator_plan_writes_readme_and_descriptor_on_submit(
    tmp_path: Path,
) -> None:
    evaluator_dir = tmp_path / "evaluator"
    evaluator_dir.mkdir(parents=True, exist_ok=True)
    (evaluator_dir / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator_dir / "labels.csv").write_text(
        "__athena_row_id,label\n1,0\n2,1\n", encoding="utf-8"
    )
    (evaluator_dir / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "HANDOFF.md").write_text(
        "prediction column: prediction\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _AgentRuntime(store, agent_id="evaluator")

    evaluator_ref = await run_evaluator_plan(
        _evaluator_runtime(tmp_path, store, agents),
        evaluator_dir,
        "write the evaluator",
        "evaluator",
        2,
    )

    assert evaluator_ref
    assert agents.created == ["evaluator"]
    assert agents.feedback == []
    # The only "freeze" is the README prompt-level marker.
    assert (evaluator_dir / "README.md").is_file()
    assert "Do NOT modify" in (evaluator_dir / "README.md").read_text(encoding="utf-8")
    descriptor = EvaluatorDescriptor.model_validate_json(
        await store.get_text(evaluator_ref)
    )
    assert descriptor.dir_path == str(evaluator_dir.resolve())
    assert descriptor.entrypoint == "evaluate.py"


@pytest.mark.asyncio
async def test_invalid_evaluator_decision_requests_compact_json(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    evaluator_dir.mkdir(parents=True)
    (evaluator_dir / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator_dir / "labels.csv").write_text(
        "__athena_row_id,label\n1,0\n2,1\n", encoding="utf-8"
    )
    (evaluator_dir / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "HANDOFF.md").write_text(
        "prediction column: prediction\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _InvalidFirstDecisionRuntime(store, agent_id="evaluator")

    assert await run_evaluator_plan(
        _evaluator_runtime(tmp_path, store, agents),
        evaluator_dir,
        "write the evaluator",
        "evaluator",
        2,
    )
    assert "Do not run tools again" in agents.feedback[0]
    assert "reason under 120 characters" in agents.feedback[0]


@pytest.mark.asyncio
async def test_evaluator_plan_missing_metric_retries(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _AgentRuntime(store, agent_id="evaluator")

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_evaluator_plan(
            _evaluator_runtime(tmp_path, store, agents),
            evaluator_dir,
            "write the evaluator",
            "evaluator",
            2,
        )

    assert agents.created == ["evaluator"]
    # 反馈必须点名**哪个目录**缺文件。只说 "metric.json is missing" 时，agent 会
    # 去看它刚列过的那个目录（可能根本不是它的 workspace），发现文件都在，于是
    # 原样再交一次——真机上就这样连交 10 次直到预算耗尽。
    assert len(agents.feedback) == 1
    assert "metric.json is missing from your workspace" in agents.feedback[0]
    assert str(evaluator_dir) in agents.feedback[0]

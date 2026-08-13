"""Deterministic PREPARE phase boundary tests."""

import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.execution.runtime import CommandResult
from athena.research.contracts import CandidateEvaluation, DataScriptBundle
from athena.research.supervisor.prepare import _freeze_evaluator, run_prepare_plan


class _AgentRuntime:
    def __init__(self, store: LocalArtifactStore) -> None:
        self.store = store
        self.created: list[str] = []
        self.feedback: list[str] = []
        self._responses: dict[str, str] = {}
        self._next = 0

    async def _response(self, run_id: str) -> None:
        decision_ref = await self.store.put_text(
            json.dumps({"decision": "submit", "reason": "ready", "suggestions": []})
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
        assert agent_id == "prepare"
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


class _Scripts:
    async def freeze(self, workspace, metadata):
        assert Path(workspace).name == "evaluator"
        assert metadata.entrypoint == "eval.py"
        return DataScriptBundle(
            bundle_id="bundle-eval",
            entrypoint="eval.py",
            lock_ref="sha256:" + "1" * 64,
            project_ref="sha256:" + "2" * 64,
            source_ref="sha256:" + "3" * 64,
            tree_ref="sha256:" + "4" * 64,
            python_version="3.12",
            environment_hash="5" * 64,
        )


class _DirScripts:
    """Freeze stub asserting the directory form resolves to evaluate.py."""

    async def freeze(self, workspace, metadata):
        assert Path(workspace).name == "evaluator"
        assert metadata.entrypoint == "evaluate.py"
        return DataScriptBundle(
            bundle_id="bundle-eval",
            entrypoint="evaluate.py",
            lock_ref="sha256:" + "1" * 64,
            project_ref="sha256:" + "2" * 64,
            source_ref="sha256:" + "3" * 64,
            tree_ref="sha256:" + "4" * 64,
            python_version="3.12",
            environment_hash="5" * 64,
        )


class _TreeScripts:
    """Freeze stub that builds a real tree manifest (rel path -> content ref)."""

    def __init__(self, store: LocalArtifactStore) -> None:
        self.store = store

    async def freeze(self, workspace, metadata):
        assert Path(workspace).name == "evaluator"
        assert metadata.entrypoint == "evaluate.py"
        tree: dict[str, str] = {}
        for path in sorted(Path(workspace).rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(workspace).as_posix()
            tree[rel] = await self.store.put_bytes(path.read_bytes())
        tree_ref = await self.store.put_text(json.dumps(tree, ensure_ascii=False))
        return DataScriptBundle(
            bundle_id="bundle-eval",
            entrypoint="evaluate.py",
            lock_ref="sha256:" + "1" * 64,
            project_ref="sha256:" + "2" * 64,
            source_ref="sha256:" + "3" * 64,
            tree_ref=tree_ref,
            python_version="3.12",
            environment_hash="5" * 64,
        )


class _Execution:
    def __init__(self, root: Path, *, fail_eval: bool = False) -> None:
        self.project_root = root
        self.environment_root = root
        self.fail_eval = fail_eval

    def ensure_environment(self) -> None:
        pass

    async def run(self, context, command=None, *, argv=None, **kwargs):
        del context, command, kwargs
        if self.fail_eval and argv and "eval" in argv[1]:
            return CommandResult(ok=False, stdout="", stderr="boom", exit_code=1)
        return CommandResult(
            ok=True, stdout='{"primary": 0.75}', stderr="", exit_code=0
        )


class _Evaluator:
    async def score(self, **kwargs):
        del kwargs
        return CandidateEvaluation(
            candidate_id="prepare", test_score=0.75, direction="maximize"
        )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing", "expected_error"),
    [
        ("evaluator", "metric.json is missing"),
        ("report", "PREPARE requires a declared, non-empty report output"),
        ("predictions", "missing predictions directory: outputs/predictions"),
        ("evidence", "trusted evidence is missing"),
        ("commit", "trusted commit is missing"),
    ],
)
async def test_prepare_result_requires_every_trusted_artifact(
    tmp_path: Path, missing: str, expected_error: str
) -> None:
    workspace_path = tmp_path / "prepare"
    (workspace_path / "evaluator").mkdir(parents=True)
    (workspace_path / "evaluator" / "eval.py").write_text("pass\n", encoding="utf-8")
    (workspace_path / "evaluator" / "labels.csv").write_text(
        "id,label\n1,0\n", encoding="utf-8"
    )
    (workspace_path / "evaluator" / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
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
    if missing != "evaluator":
        (workspace_path / "metric.json").write_text(
            json.dumps({"eval_script": "evaluator/eval.py"}), encoding="utf-8"
        )
    store = _Store(tmp_path / "artifacts", missing_evidence=missing == "evidence")
    agents = _AgentRuntime(store.inner)
    tree_ref = await store.put_text('{"experiments": []}')

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_prepare_plan(
            agents=agents,
            scripts=_Scripts(),
            evaluator=_Evaluator(),
            git=_Git(missing_commit=missing == "commit"),
            workspace=GitWorkBranch(
                path=str(workspace_path), branch="prepare", base_commit="base"
            ),
            execution=_Execution(tmp_path),
            store=store,
            tree_ref=tree_ref,
            task="build baseline",
            max_turns=2,
        )

    assert agents.created == ["prepare"]
    assert agents.feedback == [expected_error]


@pytest.mark.asyncio
async def test_freeze_evaluator_accepts_labels_directory_and_handoff(
    tmp_path: Path,
) -> None:
    """Labels may be a ``labels/`` directory (no labels.csv); HANDOFF.md is bundled."""
    workspace_path = tmp_path / "prepare"
    evaluator = workspace_path / "evaluator"
    (evaluator / "labels").mkdir(parents=True)
    (evaluator / "labels" / "truth.csv").write_text("id,label\n1,0\n", encoding="utf-8")
    (evaluator / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator / "HANDOFF.md").write_text("# Handoff\n", encoding="utf-8")
    (workspace_path / "metric.json").write_text(
        json.dumps({"eval_script": "evaluator/evaluate.py"}), encoding="utf-8"
    )

    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_evaluator(
        root=workspace_path, scripts=_TreeScripts(store), store=store
    )

    bundle = DataScriptBundle.model_validate_json(await store.get_text(evaluator_ref))
    tree = json.loads(await store.get_text(bundle.tree_ref))
    assert "labels/truth.csv" in tree
    assert "HANDOFF.md" in tree


@pytest.mark.asyncio
async def test_prepare_accepts_evaluator_directory_with_evaluate_py_entrypoint(
    tmp_path: Path,
) -> None:
    """Regression: ``outputs.evaluator`` as a directory must resolve to the
    ``evaluate.py`` entrypoint instead of looping forever on a missing draft."""
    workspace_path = tmp_path / "prepare"
    (workspace_path / "evaluator").mkdir(parents=True)
    (workspace_path / "evaluator" / "evaluate.py").write_text(
        "pass\n", encoding="utf-8"
    )
    (workspace_path / "evaluator" / "labels.csv").write_text(
        "id,label\n1,0\n", encoding="utf-8"
    )
    (workspace_path / "evaluator" / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (workspace_path / "model.py").write_text("pass\n", encoding="utf-8")
    (workspace_path / "outputs" / "predictions").mkdir(parents=True)
    (workspace_path / "outputs" / "predictions" / "predictions.csv").write_text(
        "id,prediction\n1,0\n", encoding="utf-8"
    )
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
    (workspace_path / "metric.json").write_text(
        json.dumps({"eval_script": "evaluator/evaluate.py"}), encoding="utf-8"
    )
    store = _Store(tmp_path / "artifacts")
    agents = _AgentRuntime(store.inner)
    tree_ref = await store.put_text('{"experiments": []}')

    result = await run_prepare_plan(
        agents=agents,
        scripts=_DirScripts(),
        evaluator=_Evaluator(),
        git=_Git(),
        workspace=GitWorkBranch(
            path=str(workspace_path), branch="prepare", base_commit="base"
        ),
        execution=_Execution(tmp_path),
        store=store,
        tree_ref=tree_ref,
        task="build baseline",
        max_turns=2,
    )

    assert result.metric == 0.75
    assert agents.created == ["prepare"]
    assert agents.feedback == []


@pytest.mark.asyncio
async def test_prepare_eval_script_failure_retries_without_agent_repair(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "prepare"
    (workspace_path / "evaluator").mkdir(parents=True)
    (workspace_path / "evaluator" / "eval.py").write_text("pass\n", encoding="utf-8")
    (workspace_path / "evaluator" / "labels.csv").write_text(
        "id,label\n1,0\n", encoding="utf-8"
    )
    (workspace_path / "evaluator" / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (workspace_path / "model.py").write_text("pass\n", encoding="utf-8")
    (workspace_path / "outputs" / "predictions").mkdir(parents=True)
    (workspace_path / "outputs" / "predictions" / "predictions.csv").write_text(
        "id,prediction\n1,0\n", encoding="utf-8"
    )
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
    (workspace_path / "metric.json").write_text(
        json.dumps({"eval_script": "evaluator/eval.py"}), encoding="utf-8"
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _AgentRuntime(store)
    tree_ref = await store.put_text('{"experiments": []}')

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_prepare_plan(
            agents=agents,
            scripts=_Scripts(),
            evaluator=_Evaluator(),
            git=_Git(),
            workspace=GitWorkBranch(
                path=str(workspace_path), branch="prepare", base_commit="base"
            ),
            execution=_Execution(tmp_path, fail_eval=True),
            store=store,
            tree_ref=tree_ref,
            task="build baseline",
            max_turns=2,
        )

    assert agents.created == ["prepare"]
    assert agents.feedback == [
        "metric.json eval script failed or produced no primary score"
    ]

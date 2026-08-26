"""Deterministic PREPARE phase boundary tests."""

import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.execution.runtime import CommandResult
from athena.research.contracts import CandidateEvaluation, DataScriptBundle
from athena.research.supervisor.prepare import (
    _freeze_evaluator,
    run_evaluator_plan,
    run_prepare_plan,
)


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

    async def reap(self, agent_id: str) -> None:
        del agent_id


class _Scripts:
    """Freeze stub asserting the evaluator directory freezes to evaluate.py."""

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
    def __init__(self, root: Path) -> None:
        self.project_root = root
        self.environment_root = root

    def ensure_environment(self) -> None:
        pass

    async def run(self, context, command=None, *, argv=None, **kwargs):
        del context, command, argv, kwargs
        return CommandResult(ok=True, stdout="", stderr="", exit_code=0)


class _Evaluator:
    async def score(self, **kwargs):
        del kwargs
        return CandidateEvaluation(
            candidate_id="prepare", test_score=0.75, direction="maximize"
        )


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


async def _frozen_evaluator_ref(store) -> str:
    return await store.put_text(
        DataScriptBundle(
            bundle_id="bundle-eval", entrypoint="evaluate.py"
        ).model_dump_json()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing", "expected_error"),
    [
        ("report", "PREPARE requires a declared, non-empty report output"),
        ("predictions", "missing predictions directory: outputs/predictions"),
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
    evaluator_ref = await _frozen_evaluator_ref(store)
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
        )

    assert agents.created == ["prepare"]
    assert agents.feedback == [expected_error]


@pytest.mark.asyncio
async def test_prepare_returns_result_on_trusted_baseline(tmp_path: Path) -> None:
    workspace_path = tmp_path / "eda"
    _write_eda_workspace(workspace_path, missing=None)
    store = _Store(tmp_path / "artifacts")
    agents = _AgentRuntime(store.inner)
    evaluator_ref = await _frozen_evaluator_ref(store)
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
    evaluator_ref = await _frozen_evaluator_ref(store)
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


@pytest.mark.asyncio
async def test_labels_without_a_row_id_column_never_freeze(tmp_path: Path) -> None:
    """真实跑测（2026-08-16）：单列 labels.csv 让每个候选都恒定得 AUC≈0.502。

    agent 交的 labels.csv 只有一列 ``label``（1200 行留出集），候选交的是 6000 行
    ``row_id,probability``，evaluate.py 把两边截到较短长度后逐位比较——比的是毫不相干
    的行。同一份预测按 row_id 正确 join 出来是 0.8668。没有 id 列时 join 在结构上就不
    可能，所以这一条必须在冻结前拦下，而不是让 SEARCH 跑完全程给出无意义的判决。
    """
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "label\n0\n1\n")
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="__athena_row_id"):
        await _freeze_evaluator(
            root=evaluator_dir, scripts=_TreeScripts(store), store=store
        )


@pytest.mark.asyncio
async def test_labels_with_wrong_row_id_column_never_freeze(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "id,label\n0,0\n1,1\n")
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="__athena_row_id"):
        await _freeze_evaluator(
            root=evaluator_dir, scripts=_TreeScripts(store), store=store
        )


@pytest.mark.asyncio
async def test_labels_carrying_a_row_id_column_freeze_normally(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "__athena_row_id,label\n0,0\n1,1\n")
    store = LocalArtifactStore(tmp_path / "artifacts")

    evaluator_ref = await _freeze_evaluator(
        root=evaluator_dir, scripts=_TreeScripts(store), store=store
    )

    bundle = DataScriptBundle.model_validate_json(await store.get_text(evaluator_ref))
    assert "labels.csv" in json.loads(await store.get_text(bundle.tree_ref))


@pytest.mark.asyncio
async def test_freeze_evaluator_accepts_labels_directory_and_handoff(
    tmp_path: Path,
) -> None:
    """Labels may be a ``labels/`` directory (no labels.csv); HANDOFF.md is bundled."""
    evaluator_dir = tmp_path / "evaluator"
    (evaluator_dir / "labels").mkdir(parents=True)
    (evaluator_dir / "labels" / "truth.csv").write_text(
        "__athena_row_id,label\n1,0\n", encoding="utf-8"
    )
    (evaluator_dir / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator_dir / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "HANDOFF.md").write_text("# Handoff\n", encoding="utf-8")
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )

    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_evaluator(
        root=evaluator_dir, scripts=_TreeScripts(store), store=store
    )

    bundle = DataScriptBundle.model_validate_json(await store.get_text(evaluator_ref))
    tree = json.loads(await store.get_text(bundle.tree_ref))
    assert "labels/truth.csv" in tree
    assert "HANDOFF.md" in tree


@pytest.mark.asyncio
async def test_freeze_evaluator_rejects_labels_directory_without_row_id(
    tmp_path: Path,
) -> None:
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
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="__athena_row_id"):
        await _freeze_evaluator(
            root=evaluator_dir, scripts=_TreeScripts(store), store=store
        )


@pytest.mark.asyncio
async def test_freeze_evaluator_accepts_eval_script_directory(tmp_path: Path) -> None:
    """``eval_script`` may name a directory whose entrypoint is evaluate.py."""
    evaluator_dir = tmp_path / "evaluator"
    inner = evaluator_dir / "evaluator"
    inner.mkdir(parents=True)
    (inner / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (inner / "labels.csv").write_text("__athena_row_id,label\n1,0\n", encoding="utf-8")
    (inner / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluator"}), encoding="utf-8"
    )

    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_evaluator(
        root=evaluator_dir, scripts=_Scripts(), store=store
    )

    assert evaluator_ref


@pytest.mark.asyncio
async def test_evaluator_plan_freezes_on_submit(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    evaluator_dir.mkdir(parents=True, exist_ok=True)
    (evaluator_dir / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (evaluator_dir / "labels.csv").write_text("__athena_row_id,label\n1,0\n", encoding="utf-8")
    (evaluator_dir / "pyproject.toml").write_text(
        "[project]\nname='eval'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (evaluator_dir / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _AgentRuntime(store, agent_id="evaluator")

    evaluator_ref = await run_evaluator_plan(
        agents=agents,
        scripts=_Scripts(),
        store=store,
        evaluator_dir=evaluator_dir,
        execution=_Execution(tmp_path),
        task="write the evaluator",
        max_turns=2,
    )

    assert evaluator_ref
    assert agents.created == ["evaluator"]
    assert agents.feedback == []


@pytest.mark.asyncio
async def test_evaluator_plan_missing_metric_retries(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _AgentRuntime(store, agent_id="evaluator")

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_evaluator_plan(
            agents=agents,
            scripts=_Scripts(),
            store=store,
            evaluator_dir=evaluator_dir,
            execution=_Execution(tmp_path),
            task="write the evaluator",
            max_turns=2,
        )

    assert agents.created == ["evaluator"]
    assert agents.feedback == ["metric.json is missing"]

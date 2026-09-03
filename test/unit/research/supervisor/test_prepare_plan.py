"""Deterministic PREPARE phase boundary tests."""

import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.execution.runtime import CommandResult
from athena.research.contracts import CandidateEvaluation, EvaluatorDescriptor
from athena.research.supervisor.prepare import (
    _validate_frozen_evaluator,
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
    """Script runner stub: no freeze or run_dir, so property tests are skipped."""


class _Execution:
    def __init__(self, root: Path) -> None:
        self.project_root = root
        self.environment_root = root

    def ensure_environment(self) -> None:
        pass

    async def run(self, context, request):
        del context
        _produce_declared_outputs(request.workdir)
        return CommandResult(ok=True, stdout="", stderr="", exit_code=0)


# 声明产物在命令跑之前会被新鲜度守卫归档走（42748ff）。真实命令会把它们写回来，
# 所以这个替身也必须写——否则每个用例都退化成"命令什么也没产出"。要写什么由
# fixture 记在 sidecar 里；sidecar 放在声明产物之外，归档带不走它。
_PRODUCES = "_produces.json"


def _record_produces(workdir: Path, files: dict[str, str]) -> None:
    (workdir / _PRODUCES).write_text(json.dumps(files), encoding="utf-8")


def _produce_declared_outputs(workdir: str | None) -> None:
    if workdir is None:
        return
    sidecar = Path(workdir) / _PRODUCES
    if not sidecar.is_file():
        return
    for rel, content in json.loads(sidecar.read_text(encoding="utf-8")).items():
        target = Path(workdir) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # 逐字节写：文本模式在 Windows 上会改写换行，而断言比的是字节。
        target.write_bytes(content.encode("utf-8"))


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
    # ``missing`` 的含义是"命令产不出这一项"，所以它不进 sidecar——守卫归档之后
    # 那一项始终缺席，正是这些用例要验的。
    produces: dict[str, str] = {}
    if missing != "predictions":
        produces["outputs/predictions/predictions.csv"] = "id,prediction\n1,0\n"
    if missing != "report":
        produces["outputs/report.md"] = "# Baseline\n"
    _record_produces(workspace_path, produces)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing", "expected_error"),
    [
        # report / predictions 现在由新鲜度守卫先拦下（42748ff）：它报的是"命令
        # 跑完了但什么也没产出"，比原来那句"缺少某某输出"更准确地说明了发生什么。
        ("report", "report output produced no new artifact"),
        ("predictions", "predictions output produced no new artifact"),
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
        )

    assert agents.created == ["prepare"]
    # 子串而非相等：守卫的信息里带工作区绝对路径，写死比不了。
    assert len(agents.feedback) == 1
    assert expected_error in agents.feedback[0], agents.feedback[0]


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
async def test_labels_without_row_id_are_rejected_by_validation(tmp_path: Path) -> None:
    """单列 labels.csv 仍必须在 README 冻结前被结构性检查拦下。"""
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "label\n0\n1\n")
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="__athena_row_id"):
        await _validate_frozen_evaluator(
            root=evaluator_dir, scripts=_Scripts(), store=store
        )


@pytest.mark.asyncio
async def test_labels_with_wrong_row_id_are_rejected_by_validation(
    tmp_path: Path,
) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "id,label\n0,0\n1,1\n")
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="__athena_row_id"):
        await _validate_frozen_evaluator(
            root=evaluator_dir, scripts=_Scripts(), store=store
        )


@pytest.mark.asyncio
async def test_duplicate_row_ids_are_rejected_by_validation(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(
        evaluator_dir,
        "__athena_row_id,label\n0,0\n0,1\n",
    )
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="duplicate '__athena_row_id'"):
        await _validate_frozen_evaluator(
            root=evaluator_dir, scripts=_Scripts(), store=store
        )


@pytest.mark.asyncio
async def test_valid_tabular_labels_pass_validation(tmp_path: Path) -> None:
    evaluator_dir = tmp_path / "evaluator"
    _evaluator_draft(evaluator_dir, "__athena_row_id,label\n0,0\n1,1\n")
    store = LocalArtifactStore(tmp_path / "artifacts")

    # _Scripts has no run_dir(), so behavioral probes are skipped; label layout
    # must still be accepted before the probe-skip path.
    await _validate_frozen_evaluator(
        root=evaluator_dir, scripts=_Scripts(), store=store
    )


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
    store = LocalArtifactStore(tmp_path / "artifacts")

    await _validate_frozen_evaluator(
        root=evaluator_dir, scripts=_Scripts(), store=store
    )


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
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="__athena_row_id"):
        await _validate_frozen_evaluator(
            root=evaluator_dir, scripts=_Scripts(), store=store
        )


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
    store = LocalArtifactStore(tmp_path / "artifacts")

    await _validate_frozen_evaluator(
        root=evaluator_dir, scripts=_Scripts(), store=store
    )


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
    store = LocalArtifactStore(tmp_path / "artifacts")

    # No CSV labels required: custom formats are validated by the agent's own
    # format-aware probes instead of the platform CSV property tests.
    await _validate_frozen_evaluator(
        root=evaluator_dir, scripts=_Scripts(), store=store
    )


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
    # The only "freeze" is the README prompt-level marker.
    assert (evaluator_dir / "README.md").is_file()
    assert "Do NOT modify" in (evaluator_dir / "README.md").read_text(encoding="utf-8")
    descriptor = EvaluatorDescriptor.model_validate_json(
        await store.get_text(evaluator_ref)
    )
    assert descriptor.dir_path == str(evaluator_dir.resolve())
    assert descriptor.entrypoint == "evaluate.py"


@pytest.mark.asyncio
async def test_custom_evaluator_asks_human_in_non_auto_mode(tmp_path: Path) -> None:
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
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _AgentRuntime(store, agent_id="evaluator")
    asked: list[str] = []

    async def ask_user(prompt, **_kwargs):
        asked.append(prompt)
        return "accept"

    evaluator_ref = await run_evaluator_plan(
        agents=agents,
        scripts=_Scripts(),
        store=store,
        evaluator_dir=evaluator_dir,
        execution=_Execution(tmp_path),
        task="write the evaluator",
        max_turns=2,
        ask_user=ask_user,
    )

    assert evaluator_ref
    assert asked
    assert "custom prediction format" in asked[0]


@pytest.mark.asyncio
async def test_custom_evaluator_rejected_human_does_not_advance(
    tmp_path: Path,
) -> None:
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
    store = LocalArtifactStore(tmp_path / "artifacts")
    agents = _AgentRuntime(store, agent_id="evaluator")

    async def ask_user(prompt, **_kwargs):
        del prompt
        return "reject"

    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await run_evaluator_plan(
            agents=agents,
            scripts=_Scripts(),
            store=store,
            evaluator_dir=evaluator_dir,
            execution=_Execution(tmp_path),
            task="write the evaluator",
            max_turns=1,
            ask_user=ask_user,
        )


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
    # 反馈必须点名**哪个目录**缺文件。只说 "metric.json is missing" 时，agent 会
    # 去看它刚列过的那个目录（可能根本不是它的 workspace），发现文件都在，于是
    # 原样再交一次——真机上就这样连交 10 次直到预算耗尽。
    assert len(agents.feedback) == 1
    assert "metric.json is missing from your workspace" in agents.feedback[0]
    assert str(evaluator_dir) in agents.feedback[0]

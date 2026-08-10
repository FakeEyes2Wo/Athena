"""RUN_SERVICE 静态服务注册与执行测试（supervisor_design §5）。

覆盖：白名单校验拒绝未知服务、registry 拒绝未注册服务、dataset.ingest 经
Executor 原子提交 manifest 事实（ServiceResult.facts → complete_operation）。
"""

import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.research.contracts import (
    CandidateEvaluation,
    DataScriptBundle,
    DatasetManifest,
)
from athena.research.data_service import DatasetService
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import BundleMetadata, DataScriptRunner
from athena.research.search import SearchService
from athena.research.services import ResearchServices
from athena.research.supervisor.executor import PlanExecutor
from athena.research.supervisor.journal import PlanJournal
from athena.research.supervisor.models import (
    ControlStatus,
    OperationType,
    PlanStatus,
    SupervisorOperation,
    SupervisorPlan,
)
from athena.research.supervisor.state import ProjectStateStore
from athena.research.supervisor.validator import PlanValidator, ValidationError
from test.unit.research.test_supervisor_core import FakeWorkerRuntime


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _services(tmp_path: Path, store: LocalArtifactStore) -> ResearchServices:
    return ResearchServices(
        store=store,
        dataset=DatasetService(workdir=tmp_path / ".athena" / "data"),
        runner=DataScriptRunner(store=store, workdir=tmp_path / ".athena" / "runs"),
    )


@pytest.mark.asyncio
async def test_dataset_ingest_via_executor(tmp_path: Path) -> None:
    """RUN_SERVICE 调用 dataset.ingest → 结果 ref 落库 + manifest 事实原子提交。"""
    source = tmp_path / "src"
    source.mkdir()
    (source / "d.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    journal = PlanJournal(tmp_path / ".athena" / "supervisor.db")
    state = ProjectStateStore(journal)
    execution_id = journal.create_execution()["execution_id"]
    store = LocalArtifactStore(tmp_path / "artifacts")
    executor = PlanExecutor(
        journal=journal,
        state=state,
        runtime=FakeWorkerRuntime(store),
        store=store,
        services=_services(tmp_path, store),
    )
    op = SupervisorOperation(
        operation_id="op_svc",
        operation_type=OperationType.RUN_SERVICE,
        idempotency_key="run:dataset.ingest",
        inputs={"service": "dataset.ingest", "request": {"source_root": str(source)}},
    )
    plan = SupervisorPlan(
        plan_id="plan_svc",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="PREPARE_INGEST",
        operations=[op],
        created_at=_now(),
    )
    lease = journal.claim_lease(execution_id, "test-owner")
    await executor.execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    manifest_ref = state.facts().dataset_manifest_ref
    assert manifest_ref is not None and manifest_ref.startswith("sha256:")
    manifest = json.loads(await store.get_text(manifest_ref))
    assert "d.csv" in manifest["files"]
    journal.close()


@pytest.mark.asyncio
async def test_unknown_service_rejected_by_validator(tmp_path: Path) -> None:
    """RUN_SERVICE 名称不在静态白名单 → Validator 拒绝。"""
    journal = PlanJournal(tmp_path / "supervisor.db")
    state = ProjectStateStore(journal)
    execution_id = journal.create_execution()["execution_id"]
    op = SupervisorOperation(
        operation_id="op_bad",
        operation_type=OperationType.RUN_SERVICE,
        idempotency_key="run:hack",
        inputs={"service": "dataset.evil", "request": {}},
    )
    plan = SupervisorPlan(
        plan_id="plan_bad",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="PREPARE_INGEST",
        operations=[op],
        created_at=_now(),
    )
    with pytest.raises(ValidationError, match="service not in RUN_SERVICE whitelist"):
        PlanValidator(journal, state).validate(
            plan, state.budget(), ControlStatus.RUNNING
        )
    journal.close()


@pytest.mark.asyncio
async def test_registry_rejects_unregistered_service(tmp_path: Path) -> None:
    """Task 8 前未注册的静态名称（evaluation/validation）→ 明确报错。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    services = _services(tmp_path, store)
    with pytest.raises(ValueError, match="service not registered"):
        await services.run("validation.reproduce_sota", {})


@pytest.mark.asyncio
async def test_freeze_roles_and_splits_registers_frozen_manifest(
    tmp_path: Path,
) -> None:
    """评审通过后冻结 reader/splits：登记 split 决策并更新 dataset_manifest_ref。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    services = _services(tmp_path, store)
    base = DatasetManifest(
        manifest_id="m1",
        source_root=str(tmp_path / "src"),
        files={"d.csv": "abc"},
        split_boundaries={},
    )
    result = await services.run(
        "dataset.freeze_roles_and_splits",
        {
            "manifest": base.model_dump(mode="json"),
            "roles": {"d.csv": "train"},
            "split_boundaries": {"train": ["r1", "r2"], "test": ["r3"]},
        },
    )
    assert result.facts["dataset_manifest_ref"]
    frozen = json.loads(await store.get_text(result.result_refs[0]))
    assert frozen["manifest"]["split_boundaries"]["train"] == ["r1", "r2"]
    assert frozen["roles"] == {"d.csv": "train"}


def _draft(tmp_path: Path) -> Path:
    """最小 uv 项目 draft（无外部依赖，entrypoint 声明式命名）。"""
    entrypoint = "src/inspect_anything.py"
    draft = tmp_path / "draft"
    script = draft / entrypoint
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import json, sys\n"
        "def main():\n"
        "    req = json.load(open(sys.argv[sys.argv.index('--request') + 1]))\n"
        "    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')\n"
        "    json.dump({'columns': list(req.get('data', {})), 'rows': 0}, out)\n"
        "main()\n",
        encoding="utf-8",
    )
    (draft / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'draft'\n"
        "version = '0.1.0'\n"
        "requires-python = '>=3.11'\n"
        "dependencies = []\n",
        encoding="utf-8",
    )
    return draft


@pytest.mark.asyncio
async def test_scripts_freeze_and_run_adapters(tmp_path: Path) -> None:
    """registry 的 scripts.freeze/run adapter：BundleMetadata 入参 → ServiceResult。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    services = _services(tmp_path, store)
    draft = _draft(tmp_path)
    frozen = await services.run(
        "scripts.freeze",
        {
            "workspace": str(draft),
            "metadata": {"entrypoint": "src/inspect_anything.py"},
        },
    )
    assert frozen.result_refs
    bundle = DataScriptBundle.model_validate_json(
        await store.get_text(frozen.result_refs[0])
    )
    assert bundle.runtime == "python-uv"

    ran = await services.run(
        "scripts.run",
        {
            "bundle": bundle.model_dump(mode="json"),
            "request": {"data": {"a": 1}},
            "output_schema": {"columns": None, "rows": None},
        },
    )
    assert ran.result_refs  # 运行输出已落库


@pytest.mark.asyncio
async def test_search_register_and_freeze_round_via_registry(tmp_path: Path) -> None:
    """search 服务：全部假设入图 + 按 test_score 冻结 round + 唯一 SOTA 事实。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    tree = ResearchTree()
    services = ResearchServices(
        store=store,
        dataset=DatasetService(workdir=tmp_path / ".athena" / "data"),
        runner=DataScriptRunner(store=store, workdir=tmp_path / ".athena" / "runs"),
        search=SearchService(tree),
    )
    registered = await services.run(
        "search.register_hypotheses",
        {
            "hypotheses": [
                Hypothesis(
                    statement="s1", intervention="i1", expected_effect="up"
                ).model_dump(mode="json"),
                Hypothesis(
                    statement="s2", intervention="i2", expected_effect="up"
                ).model_dump(mode="json"),
            ]
        },
    )
    assert registered.facts["graph_ref"]
    assert len(tree.pending_hypotheses()) == 2  # 全部入图

    frozen = await services.run(
        "search.freeze_ranking_round",
        {
            "parent_score": 0.8,
            "candidates": [
                {"candidate_id": "a", "test_score": 0.9, "direction": "maximize"},
                {"candidate_id": "b", "test_score": 0.7, "direction": "maximize"},
            ],
            "selected_count": 1,
        },
    )
    assert frozen.facts["ranking_round_ref"]
    assert frozen.facts["sota_experiment_ref"] == "a"  # test_score 唯一 SOTA


def _eval_draft(tmp_path: Path) -> tuple[Path, str]:
    """最小 uv eval 项目：读 predictions.csv/labels.csv 算 MAE，打印 primary。"""
    entrypoint = "eval.py"
    draft = tmp_path / "eval_draft"
    draft.mkdir(parents=True, exist_ok=True)
    (draft / entrypoint).write_text(
        "import csv, json, sys\n"
        "def main():\n"
        "    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')\n"
        "    preds = {r[0]: float(r[1]) for r in list(csv.reader(open('predictions.csv')))[1:]}\n"
        "    labels = {r[0]: float(r[1]) for r in list(csv.reader(open('labels.csv')))[1:]}\n"
        "    rows = [(preds[k], labels[k]) for k in preds if k in labels]\n"
        "    mae = sum(abs(a - b) for a, b in rows) / len(rows)\n"
        "    json.dump({'primary': mae, 'metric': 'mae'}, out)\n"
        "main()\n",
        encoding="utf-8",
    )
    (draft / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'eval'\n"
        "version = '0.1.0'\n"
        "requires-python = '>=3.11'\n"
        "dependencies = []\n",
        encoding="utf-8",
    )
    return draft, entrypoint


@pytest.mark.asyncio
async def test_trusted_evaluator_scores_aligned_predictions(tmp_path: Path) -> None:
    """trusted evaluator 运行冻结 eval bundle，对齐预测/标签产出唯一 test_score。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    draft, entrypoint = _eval_draft(tmp_path)
    eval_bundle = await runner.freeze(draft, BundleMetadata(entrypoint=entrypoint))

    evaluator = TrustedEvaluator(runner)
    result = await evaluator.score(
        eval_bundle=eval_bundle,
        predictions="__athena_row_id,prediction\nr1,0.0\nr2,1.0\n",
        labels="__athena_row_id,target\nr1,0.0\nr2,2.0\n",
        candidate_id="cand_1",
        direction="minimize",
    )
    assert result.candidate_id == "cand_1"
    assert result.test_score == pytest.approx(0.5)  # MAE of [0, 1]
    assert result.direction == "minimize"


@pytest.mark.asyncio
async def test_evaluation_baseline_via_registry(tmp_path: Path) -> None:
    """evaluation.baseline 服务：评估 baseline 并提交 baseline_experiment_ref 事实。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    draft, entrypoint = _eval_draft(tmp_path)
    eval_bundle = await runner.freeze(draft, BundleMetadata(entrypoint=entrypoint))
    services = ResearchServices(
        store=store,
        dataset=DatasetService(workdir=tmp_path / ".athena" / "data"),
        runner=runner,
        evaluator=TrustedEvaluator(runner),
    )
    result = await services.run(
        "evaluation.baseline",
        {
            "eval_bundle": eval_bundle.model_dump(mode="json"),
            "predictions": "__athena_row_id,prediction\nr1,0.0\nr2,1.0\n",
            "labels": "__athena_row_id,target\nr1,0.0\nr2,2.0\n",
            "candidate_id": "baseline",
        },
    )
    assert result.facts["baseline_experiment_ref"]
    candidate = CandidateEvaluation.model_validate_json(
        await store.get_text(result.facts["baseline_experiment_ref"])
    )
    assert candidate.test_score == pytest.approx(0.5)

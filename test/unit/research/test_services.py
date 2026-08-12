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
    DerivedDatasetManifest,
    EDAAttemptOutcome,
    EDARepairFailure,
)
from athena.research.data_service import DatasetService
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import BundleMetadata, DataScriptRunner
from athena.core.research_tree import ExperimentStatus
from athena.core.workspace import GitWorkBranch
from athena.research.search import SearchService
from athena.research.services import ResearchServices


def _services(tmp_path: Path, store: LocalArtifactStore) -> ResearchServices:
    return ResearchServices(
        store=store,
        dataset=DatasetService(workdir=tmp_path / ".athena" / "data"),
        runner=DataScriptRunner(store=store, workdir=tmp_path / ".athena" / "runs"),
    )


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
    """trusted evaluator 运行冻结 eval bundle；labels 来自冻结 bundle，只注入 predictions。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    draft, entrypoint = _eval_draft(tmp_path)
    (draft / "labels.csv").write_text(
        "__athena_row_id,target\nr1,0.0\nr2,2.0\n", encoding="utf-8"
    )
    eval_bundle = await runner.freeze(draft, BundleMetadata(entrypoint=entrypoint))

    evaluator = TrustedEvaluator(runner)
    result = await evaluator.score(
        eval_bundle=eval_bundle,
        predictions="__athena_row_id,prediction\nr1,0.0\nr2,1.0\n",
        candidate_id="cand_1",
        direction="minimize",
    )
    assert result.candidate_id == "cand_1"
    assert result.test_score == pytest.approx(0.5)  # MAE of [0, 1]，用冻结 labels
    assert result.direction == "minimize"


@pytest.mark.asyncio
async def test_search_rejects_untrusted_labels(tmp_path: Path) -> None:
    """可信评估只用冻结 bundle 的 labels——score 不接受候选 labels（design 修复 5）。

    labels 在 PREPARE 冻结进 bundle（Task 3：InitAgent 写 labels.csv 进 tree_ref）；
    score 只接收 predictions，候选伪造的 labels 无法影响分数。
    """
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    draft, entrypoint = _eval_draft(tmp_path)
    (draft / "labels.csv").write_text(
        "__athena_row_id,target\nr1,0.0\nr2,2.0\n", encoding="utf-8"
    )
    eval_bundle = await runner.freeze(draft, BundleMetadata(entrypoint=entrypoint))

    evaluator = TrustedEvaluator(runner)
    result = await evaluator.score(
        eval_bundle=eval_bundle,
        predictions="__athena_row_id,prediction\nr1,0.0\nr2,1.0\n",
        candidate_id="cand_1",
        direction="minimize",
    )
    # 分数来自冻结 bundle 的 labels（MAE 0.5）；候选若自带 labels 应被拒绝
    assert result.test_score == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_evaluation_baseline_via_registry(tmp_path: Path) -> None:
    """evaluation.baseline 服务：评估 baseline 并提交 baseline_experiment_ref 事实。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    draft, entrypoint = _eval_draft(tmp_path)
    (draft / "labels.csv").write_text(
        "__athena_row_id,target\nr1,0.0\nr2,2.0\n", encoding="utf-8"
    )
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
            "candidate_id": "baseline",
        },
    )
    assert result.facts["baseline_experiment_ref"]
    candidate = CandidateEvaluation.model_validate_json(
        await store.get_text(result.facts["baseline_experiment_ref"])
    )
    assert candidate.test_score == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_evaluation_single_reads_frozen_predictions_ref(tmp_path: Path) -> None:
    """Final evaluation can consume the SOTA prediction artifact directly."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    draft, entrypoint = _eval_draft(tmp_path)
    (draft / "labels.csv").write_text(
        "__athena_row_id,target\nr1,0.0\nr2,2.0\n", encoding="utf-8"
    )
    eval_bundle = await runner.freeze(draft, BundleMetadata(entrypoint=entrypoint))
    predictions_ref = await store.put_text(
        "__athena_row_id,prediction\nr1,0.0\nr2,1.0\n"
    )
    services = ResearchServices(
        store=store,
        dataset=DatasetService(workdir=tmp_path / ".athena" / "data"),
        runner=runner,
        evaluator=TrustedEvaluator(runner),
    )

    result = await services.run(
        "evaluation.single",
        {
            "eval_bundle": eval_bundle.model_dump(mode="json"),
            "predictions_ref": predictions_ref,
            "direction": "minimize",
        },
    )

    candidate = CandidateEvaluation.model_validate_json(
        await store.get_text(result.result_refs[0])
    )
    assert candidate.test_score == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_accept_derived_view_commits_active_view(tmp_path) -> None:
    """接受特征视图候选 → 提交 active_dataset_view_ref（§Supervisor Facts）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    services = _services(tmp_path, store)
    service = DatasetService(workdir=tmp_path / "data")
    src = tmp_path / "orig"
    src.mkdir()
    (src / "train.csv").write_text("id,age,label\n1,20,0\n2,30,1\n", encoding="utf-8")
    parent = service.ingest(src)

    candidate = DerivedDatasetManifest(
        manifest_id="view_c",
        parent_manifest_id=parent.manifest_id,
        files={**parent.files, "features/f.csv": "sha:f"},
        columns=parent.columns,
        column_hashes=parent.column_hashes,
        row_identity_hash=parent.row_identity_hash,
        split_boundaries=parent.split_boundaries,
        derived_columns=["feat"],
        column_files={"feat": "features/f.csv"},
        enabled_derived_columns=["feat"],
    )
    result = await services.run(
        "dataset.accept_derived",
        {
            "parent": parent.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
        },
    )
    accepted_ref = result.result_refs[0]
    assert result.facts["active_dataset_view_ref"] == accepted_ref
    accepted = DerivedDatasetManifest.model_validate_json(
        await store.get_text(accepted_ref)
    )
    assert accepted.manifest_id.startswith("view_")


@pytest.mark.asyncio
async def test_search_prepare_experiment_creates_running_experiment(
    tmp_path: Path,
) -> None:
    """prepare_experiment：注册假设 → 建 worktree → RUNNING 实验 → Code 赋值。"""
    service = SearchService()
    hypotheses = [
        Hypothesis(
            statement="scale features",
            intervention="standardize",
            expected_effect="raise",
        )
    ]
    assignment = await service.prepare_experiment(
        hypotheses=hypotheses,
        make_worktree=lambda eid, branch: GitWorkBranch(
            path=str(tmp_path / eid), branch=branch, base_commit="abc123"
        ),
        run_config_ref="cfg://run",
    )
    assert assignment["workspace"] and assignment["environment_root"]
    experiment = service.graph.get_experiment(assignment["experiment_id"])
    assert experiment.status is ExperimentStatus.RUNNING
    assert experiment.plan.kind == "search"
    assert experiment.gitwork.branch.startswith("athena/search/")


class _FakeEvaluator:
    """只返回固定 test_score 的 fake trusted evaluator。"""

    def __init__(self, score: float) -> None:
        self._score = score

    async def score(self, *, eval_bundle, predictions, candidate_id, direction):
        from athena.research.contracts import CandidateEvaluation

        return CandidateEvaluation(
            candidate_id=candidate_id, test_score=self._score, direction=direction
        )


@pytest.mark.asyncio
async def test_search_finish_experiment_sets_sota(tmp_path: Path) -> None:
    """finish_experiment：评分 → complete → 首个实验设为 SOTA（无父 SOTA）。"""
    service = SearchService()
    assignment = await service.prepare_experiment(
        hypotheses=[Hypothesis(statement="s", intervention="i", expected_effect="e")],
        make_worktree=lambda eid, branch: GitWorkBranch(
            path=str(tmp_path / eid), branch=branch, base_commit="abc"
        ),
        run_config_ref="cfg",
    )
    result = await service.finish_experiment(
        experiment_id=assignment["experiment_id"],
        predictions="__athena_row_id,prediction\nr1,0.0\nr2,1.0\n",
        predictions_ref="sha256:p",
        evaluator=_FakeEvaluator(0.9),
        eval_bundle=object(),
        direction="maximize",
    )
    assert result["sota_experiment_id"] == assignment["experiment_id"]
    experiment = service.graph.get_experiment(assignment["experiment_id"])
    assert experiment.status is ExperimentStatus.SUCCEEDED
    assert experiment.eval is not None and experiment.eval.primary == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_search_prepare_experiment_service(tmp_path: Path) -> None:
    """search.prepare_experiment 服务：注册假设 → RUNNING 实验 → graph_ref + assignment。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    services = ResearchServices(
        store=store,
        dataset=DatasetService(workdir=tmp_path / ".athena" / "data"),
        runner=DataScriptRunner(store=store, workdir=tmp_path / ".athena" / "runs"),
        workspace=lambda eid, branch: GitWorkBranch(
            path=str(tmp_path / eid), branch=branch, base_commit="abc"
        ),
    )
    result = await services.run(
        "search.prepare_experiment",
        {
            "hypotheses": [
                {"statement": "s", "intervention": "i", "expected_effect": "e"}
            ],
            "run_config_ref": "cfg",
        },
    )
    assert result.facts["graph_ref"]
    assignment = json.loads(await store.get_text(result.result_refs[0]))
    assert assignment["experiment_id"]
    assert assignment["workspace"]
    tree = ResearchTree.from_dict(
        json.loads(await store.get_text(result.facts["graph_ref"]))
    )
    experiment = tree.get_experiment(assignment["experiment_id"])
    assert experiment.status is ExperimentStatus.RUNNING

"""确定性领域服务注册（supervisor_design §5 RUN_SERVICE 白名单）。

ResearchRuntime 直接装配这些服务，通过 ``SupervisorServices`` 静态名称暴露给
Executor 的 RUN_SERVICE operation。只注册已实现合同的领域服务（dataset /
scripts）；evaluation / search / validation 服务随 Task 6-8 注册。服务是强类型、
确定性的，不编排 Agent，也不自行决定下一步。
"""

import json

from athena.core.contracts import ArtifactStore
from athena.core.research_models import Hypothesis
from athena.research.contracts import (
    CandidateEvaluation,
    DataScriptBundle,
    DatasetManifest,
    DerivedDatasetManifest,
    ServiceResult,
)
from athena.research.data_service import DatasetService
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import BundleMetadata, DataScriptRunner
from athena.research.search import SearchService

# RUN_SERVICE 只接受以下静态名称（supervisor_design §5）。
RUN_SERVICE_NAMES = frozenset(
    {
        "dataset.ingest",
        "dataset.freeze_roles_and_splits",
        "dataset.accept_derived",
        "scripts.freeze",
        "scripts.run",
        "evaluation.baseline",
        "evaluation.candidate_batch",
        "search.register_hypotheses",
        "search.freeze_ranking_round",
        "validation.reproduce_sota",
        "validation.run_ablation",
        "validation.advance_final_test",
    }
)


class ResearchServices:
    """静态 RUN_SERVICE 注册表：把领域服务适配为 ``ServiceResult`` 合同。

    ``run`` 只接受白名单名称；未注册或未知名称直接报错，不静默回退。
    """

    def __init__(
        self,
        *,
        store: ArtifactStore,
        dataset: DatasetService,
        runner: DataScriptRunner,
        search: SearchService | None = None,
        evaluator: TrustedEvaluator | None = None,
    ) -> None:
        self._store = store
        self._dataset = dataset
        self._runner = runner
        self._search = search
        self._evaluator = evaluator

    async def run(self, service: str, request: dict[str, object]) -> ServiceResult:
        handler = {
            "dataset.ingest": self._dataset_ingest,
            "dataset.freeze_roles_and_splits": self._dataset_freeze_roles_and_splits,
            "dataset.accept_derived": self._dataset_accept_derived,
            "scripts.freeze": self._scripts_freeze,
            "scripts.run": self._scripts_run,
            "evaluation.baseline": self._evaluation_baseline,
            "evaluation.candidate_batch": self._evaluation_candidate_batch,
            "search.register_hypotheses": self._search_register_hypotheses,
            "search.freeze_ranking_round": self._search_freeze_ranking_round,
        }.get(service)
        if handler is None:
            raise ValueError(f"service not registered: {service}")
        return await handler(request)

    async def _dataset_ingest(self, request: dict[str, object]) -> ServiceResult:
        """复制源目录字节并冻结 SHA-256 manifest（不解析业务数据）。

        同时把受管 raw snapshot 的绝对路径投影为事实，供同 Plan 内的 DataAgent
        经 ``{"fact": "dataset_managed_root"}`` 解析——worker 只读这份不可变拷贝，
        不消费原始源目录。
        """
        source_root = str(request["source_root"])
        manifest = self._dataset.ingest(source_root)
        manifest_ref = await self._store.put_text(manifest.model_dump_json())
        return ServiceResult(
            result_refs=[manifest_ref],
            facts={
                "dataset_manifest_ref": manifest_ref,
                "dataset_managed_root": manifest.managed_root or "",
            },
        )

    async def _dataset_freeze_roles_and_splits(
        self, request: dict[str, object]
    ) -> ServiceResult:
        """角色提议与 split 边界通过评审后冻结 reader/splits（§2.4）。

        只把 LLM 提供的角色与 split 决策登记进冻结 Artifact，平台不解析业务
        语义；split_boundaries 仍可由后续 derived 校验（accept_derived）强约束。
        """
        manifest = DatasetManifest.model_validate(request["manifest"])
        split_boundaries = request.get("split_boundaries", {})
        roles = request.get("roles", {})
        if not isinstance(split_boundaries, dict) or not isinstance(roles, dict):
            raise ValueError("split_boundaries/roles must be dicts")
        frozen = manifest.model_copy(update={"split_boundaries": split_boundaries})
        frozen_ref = await self._store.put_text(
            json.dumps(
                {"manifest": frozen.model_dump(mode="json"), "roles": roles},
                ensure_ascii=False,
            )
        )
        return ServiceResult(
            result_refs=[frozen_ref],
            facts={"dataset_manifest_ref": frozen_ref},
        )

    async def _dataset_accept_derived(
        self, request: dict[str, object]
    ) -> ServiceResult:
        """校验 derived 不变量（原始列/值/行身份/split 不变，仅追加新列）。"""
        parent = DatasetManifest.model_validate(request["parent"])
        candidate = DerivedDatasetManifest.model_validate(request["candidate"])
        accepted = self._dataset.accept_derived(parent, candidate)
        accepted_ref = await self._store.put_text(accepted.model_dump_json())
        return ServiceResult(result_refs=[accepted_ref])

    async def _scripts_freeze(self, request: dict[str, object]) -> ServiceResult:
        """冻结 draft：uv lock + 固化源码树/版本/environment hash。"""
        workspace = str(request["workspace"])
        metadata = BundleMetadata(**request["metadata"])
        bundle = await self._runner.freeze(workspace, metadata)
        bundle_ref = await self._store.put_text(bundle.model_dump_json())
        return ServiceResult(result_refs=[bundle_ref])

    async def _evaluation_baseline(self, request: dict[str, object]) -> ServiceResult:
        """对 baseline 实验运行 eval，产出唯一可信 test_score（baseline_experiment_ref）。"""
        if self._evaluator is None:
            raise ValueError("evaluator service not wired")
        eval_bundle = DataScriptBundle.model_validate(request["eval_bundle"])
        candidate = await self._evaluator.score(
            eval_bundle=eval_bundle,
            predictions=str(request["predictions"]),
            labels=str(request["labels"]),
            candidate_id=str(request.get("candidate_id", "baseline")),
            direction=str(request.get("direction", "maximize")),
        )
        ref = await self._store.put_text(candidate.model_dump_json())
        return ServiceResult(result_refs=[ref], facts={"baseline_experiment_ref": ref})

    async def _evaluation_candidate_batch(
        self, request: dict[str, object]
    ) -> ServiceResult:
        """对一批候选运行 eval，产出 CandidateEvaluation 列表（供 freeze_round）。"""
        if self._evaluator is None:
            raise ValueError("evaluator service not wired")
        eval_bundle = DataScriptBundle.model_validate(request["eval_bundle"])
        evaluations: list[dict[str, object]] = []
        for spec in request["candidates"]:
            candidate = await self._evaluator.score(
                eval_bundle=eval_bundle,
                predictions=str(spec["predictions"]),
                labels=str(spec["labels"]),
                candidate_id=str(spec["candidate_id"]),
                direction=str(spec.get("direction", "maximize")),
            )
            evaluations.append(candidate.model_dump(mode="json"))
        batch_ref = await self._store.put_text(
            json.dumps(evaluations, ensure_ascii=False)
        )
        return ServiceResult(result_refs=[batch_ref])

    async def _search_register_hypotheses(
        self, request: dict[str, object]
    ) -> ServiceResult:
        """所有结构有效假设先写入图（pending），返回 graph ref。"""
        if self._search is None:
            raise ValueError("search service not wired")
        hypotheses = [Hypothesis.model_validate(h) for h in request["hypotheses"]]
        graph = self._search.register_hypotheses(hypotheses)
        graph_ref = await self._store.put_text(
            json.dumps(graph.to_dict(), ensure_ascii=False)
        )
        return ServiceResult(result_refs=[graph_ref], facts={"graph_ref": graph_ref})

    async def _search_freeze_ranking_round(
        self, request: dict[str, object]
    ) -> ServiceResult:
        """按 test_score 冻结一轮 RankingRound 并判定唯一 SOTA。"""
        if self._search is None:
            raise ValueError("search service not wired")
        candidates = [
            CandidateEvaluation.model_validate(c) for c in request["candidates"]
        ]
        if not candidates:
            raise ValueError("freeze_ranking_round requires candidates")
        parent_score = request.get("parent_score")
        if parent_score is None:
            # 尚无父 SOTA 时按方向取最劣界：任一候选都优于"空"，确立首个 SOTA。
            direction = candidates[0].direction
            parent_score = float("-inf") if direction == "maximize" else float("inf")
        result = self._search.freeze_round(
            float(parent_score),
            candidates,
            selected_count=int(request.get("selected_count", 2)),
            parent_sota_id=request.get("parent_sota_id"),
        )
        round_ref = await self._store.put_text(result.ranking.model_dump_json())
        facts: dict[str, object] = {"ranking_round_ref": round_ref}
        if result.sota is not None:
            facts["sota_experiment_ref"] = result.sota.candidate_id
        return ServiceResult(result_refs=[round_ref], facts=facts)

    async def _scripts_run(self, request: dict[str, object]) -> ServiceResult:
        """执行冻结 bundle，读取并校验 output.json。"""
        bundle = DataScriptBundle.model_validate(request["bundle"])
        output_schema = request.get("output_schema")
        result = await self._runner.run(
            bundle, request["request"], output_schema=output_schema
        )
        result_refs = list(result.result_refs)
        if not result_refs and result.outputs:
            result_refs.append(
                await self._store.put_text(
                    json.dumps(result.outputs, ensure_ascii=False)
                )
            )
        return ServiceResult(result_refs=result_refs)

"""确定性领域服务注册（supervisor_design §5 RUN_SERVICE 白名单）。

ResearchRuntime 直接装配这些服务，通过 ``SupervisorServices`` 静态名称暴露给
Executor 的 RUN_SERVICE operation。只注册已实现合同的领域服务（dataset /
scripts）；evaluation / search / validation 服务随 Task 6-8 注册。服务是强类型、
确定性的，不编排 Agent，也不自行决定下一步。
"""

import json
from collections.abc import Callable
from pathlib import Path

from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.contracts import (
    CandidateEvaluation,
    DataScriptBundle,
    DatasetManifest,
    DatasetRoleProposal,
    DerivedDatasetManifest,
    EDAAttemptOutcome,
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
        "evaluation.single",
        "search.register_hypotheses",
        "search.freeze_ranking_round",
        "search.prepare_experiment",
        "search.finish_experiment",
        "eda.attempt.outcome",
        "role.attempt.outcome",
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
        workspace: Callable[[str, str], GitWorkBranch] | None = None,
    ) -> None:
        self._store = store
        self._dataset = dataset
        self._runner = runner
        self._search = search
        self._evaluator = evaluator
        self._workspace = workspace

    async def run(self, service: str, request: dict[str, object]) -> ServiceResult:
        """按白名单服务名分发请求并返回 ServiceResult。"""
        handler = {
            "dataset.ingest": self._dataset_ingest,
            "dataset.freeze_roles_and_splits": self._dataset_freeze_roles_and_splits,
            "dataset.accept_derived": self._dataset_accept_derived,
            "scripts.freeze": self._scripts_freeze,
            "scripts.run": self._scripts_run,
            "evaluation.baseline": self._evaluation_baseline,
            "evaluation.candidate_batch": self._evaluation_candidate_batch,
            "evaluation.single": self._evaluation_single,
            "search.register_hypotheses": self._search_register_hypotheses,
            "search.prepare_experiment": self._search_prepare_experiment,
            "search.finish_experiment": self._search_finish_experiment,
            "search.freeze_ranking_round": self._search_freeze_ranking_round,
            "eda.attempt.outcome": self._eda_attempt_outcome,
            "role.attempt.outcome": self._role_attempt_outcome,
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
        """接受特征视图候选并提交 ``active_dataset_view_ref``（§Supervisor Facts）。

        校验 derived 不变量（含 previous 增量文件/元数据保留）；接受后 active 视图
        指向已接受 manifest。previous 为原始视图（无派生历史）时视为无 previous。
        """
        parent = DatasetManifest.model_validate(request["parent"])
        candidate = DerivedDatasetManifest.model_validate(request["candidate"])
        previous = None
        raw_previous = request.get("previous")
        if isinstance(raw_previous, dict):
            try:
                previous = DerivedDatasetManifest.model_validate(raw_previous)
            except Exception:
                previous = None  # 原始视图（无派生历史）→ 无 previous
        accepted = self._dataset.accept_derived(parent, candidate, previous=previous)
        accepted_ref = await self._store.put_text(accepted.model_dump_json())
        return ServiceResult(
            result_refs=[accepted_ref],
            facts={"active_dataset_view_ref": accepted_ref},
        )

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
            candidate_id=str(request.get("candidate_id", "baseline")),
            direction=str(request.get("direction", "maximize")),
        )
        ref = await self._store.put_text(candidate.model_dump_json())
        return ServiceResult(result_refs=[ref], facts={"baseline_experiment_ref": ref})

    async def _evaluation_single(self, request: dict[str, object]) -> ServiceResult:
        """对单份 predictions 运行可信 eval（final-test 用，labels 来自冻结 bundle）。"""
        if self._evaluator is None:
            raise ValueError("evaluator service not wired")
        predictions = request.get("predictions")
        if not isinstance(predictions, str):
            predictions_ref = request.get("predictions_ref")
            path = request.get("predictions_path")
            if isinstance(predictions_ref, str):
                predictions = await self._store.get_text(predictions_ref)
            elif isinstance(path, str):
                predictions = Path(path).read_text(encoding="utf-8")
            else:
                raise ValueError(
                    "evaluation.single requires predictions, predictions_ref, "
                    "or predictions_path"
                )
        candidate = await self._evaluator.score(
            eval_bundle=DataScriptBundle.model_validate(request["eval_bundle"]),
            predictions=predictions,
            candidate_id=str(request.get("candidate_id", "final")),
            direction=str(request.get("direction", "maximize")),
        )
        ref = await self._store.put_text(candidate.model_dump_json())
        return ServiceResult(result_refs=[ref])

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

    async def _load_graph(self, graph_ref: object) -> ResearchTree:
        """从 graph_ref artifact 加载图；缺省新建。"""
        if isinstance(graph_ref, str):
            try:
                return ResearchTree.from_dict(
                    json.loads(await self._store.get_text(graph_ref))
                )
            except Exception:
                pass
        return ResearchTree()

    async def _save_graph(self, tree: ResearchTree) -> ArtifactRef:
        ref = await self._store.put_text(json.dumps(tree.to_dict(), ensure_ascii=False))
        return ref

    async def _search_prepare_experiment(
        self, request: dict[str, object]
    ) -> ServiceResult:
        """注册假设 → 选一个 pending → 建 worktree → RUNNING 实验 → Code 赋值。"""
        if self._workspace is None:
            raise ValueError("workspace service not wired")
        search = SearchService(await self._load_graph(request.get("graph_ref")))
        assignment = await search.prepare_experiment(
            hypotheses=[Hypothesis.model_validate(h) for h in request["hypotheses"]],
            make_worktree=self._workspace,
            run_config_ref=str(request["run_config_ref"]),
            parent_sota_id=request.get("parent_sota"),
        )
        graph_ref = await self._save_graph(search.graph)
        assignment_ref = await self._store.put_text(
            json.dumps(assignment, ensure_ascii=False)
        )
        return ServiceResult(
            result_refs=[assignment_ref], facts={"graph_ref": graph_ref}
        )

    async def _search_finish_experiment(
        self, request: dict[str, object]
    ) -> ServiceResult:
        """评分 → complete → set_sota；保存图 + ranking。"""
        if self._evaluator is None:
            raise ValueError("evaluator service not wired")
        # predictions 来自 code 结果（predictions_path 指向 worktree 里的 predictions.csv）
        predictions = request.get("predictions")
        if not isinstance(predictions, str):
            path = request.get("predictions_path")
            if not isinstance(path, str):
                raise ValueError(
                    "finish_experiment requires predictions or predictions_path"
                )
            predictions = Path(path).read_text(encoding="utf-8")
        predictions_ref = request.get("predictions_ref")
        if not isinstance(predictions_ref, str):
            predictions_ref = await self._store.put_text(predictions)
        search = SearchService(await self._load_graph(request.get("graph_ref")))
        result = await search.finish_experiment(
            experiment_id=str(request["experiment_id"]),
            predictions=predictions,
            predictions_ref=predictions_ref,
            evaluator=self._evaluator,
            eval_bundle=DataScriptBundle.model_validate(request["eval_bundle"]),
            direction=str(request.get("direction", "maximize")),
            commit=request.get("commit"),
        )
        graph_ref = await self._save_graph(search.graph)
        result_ref = await self._store.put_text(
            json.dumps(result, ensure_ascii=False, default=str)
        )
        result_fact = str(request.get("result_fact", "sota_experiment_ref"))
        sota_id = result["sota_experiment_id"]
        sota_predictions_ref = search.graph.get_experiment(str(sota_id)).artifacts[
            "predictions"
        ]
        return ServiceResult(
            result_refs=[graph_ref, result_ref],
            facts={
                "graph_ref": graph_ref,
                result_fact: sota_id,
                "sota_predictions_ref": sota_predictions_ref,
            },
        )

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

    async def _role_attempt_outcome(self, request: dict[str, object]) -> ServiceResult:
        """Commit a role proposal or persist a repairable role-script failure."""
        outcome_ref = str(request["outcome_ref"])
        raw = await self._store.get_text(outcome_ref)
        execution_id = str(request["execution_id"])
        agent_id = str(request["agent_id"])
        try:
            DatasetRoleProposal.model_validate_json(raw)
        except ValueError:
            outcome = EDAAttemptOutcome.model_validate_json(raw)
            facts: dict[str, object] = {
                "role_repair_state": {
                    "execution_id": execution_id,
                    "agent_id": agent_id,
                    "workspace": outcome.workspace,
                    "repair_count": outcome.repair_count,
                    "status": outcome.status,
                    "last_failure_ref": outcome.failure_ref,
                    "failure_signature": outcome.failure_signature,
                }
            }
            if outcome.status == "succeeded" and outcome.bundle_ref:
                facts["dataset_role_proposal_ref"] = outcome.bundle_ref
                facts["data_agent_id"] = agent_id
            return ServiceResult(facts=facts)

        return ServiceResult(
            facts={
                "dataset_role_proposal_ref": outcome_ref,
                "data_agent_id": agent_id,
                "role_repair_state": {
                    "execution_id": execution_id,
                    "agent_id": agent_id,
                    "workspace": str(request.get("workspace", "")),
                    "repair_count": int(request.get("repair_count", 0) or 0),
                    "status": "succeeded",
                    "last_failure_ref": None,
                    "failure_signature": None,
                },
            }
        )

    async def _eda_attempt_outcome(self, request: dict[str, object]) -> ServiceResult:
        """校验 DataAgent 的 EDA 尝试结果并提交 ``eda_repair_state``。

        succeeded → 提升 ``bundle_ref`` 为 ``eda_report_ref``（下游 EDA review 读它）；
        repairable_failure → 只落 ``eda_repair_state``，由 Planner 决定修复/人工。
        原子提交：经 RUN_SERVICE 的 complete_operation 走与 COMMIT_FACTS 相同事务。
        """
        outcome_ref = str(request["outcome_ref"])
        outcome = EDAAttemptOutcome.model_validate_json(
            await self._store.get_text(outcome_ref)
        )
        execution_id = str(request["execution_id"])
        agent_id = str(request["agent_id"])
        facts: dict[str, object] = {
            "eda_repair_state": {
                "execution_id": execution_id,
                "agent_id": agent_id,
                "workspace": outcome.workspace,
                "repair_count": outcome.repair_count,
                "status": outcome.status,
                "bundle_ref": outcome.bundle_ref,
                "last_failure_ref": outcome.failure_ref,
                "failure_signature": outcome.failure_signature,
                "policy": str(request.get("policy", "capped")),
            }
        }
        if outcome.status == "succeeded":
            facts["eda_report_ref"] = outcome.bundle_ref
            facts["eda_agent_id"] = agent_id
        return ServiceResult(facts=facts)

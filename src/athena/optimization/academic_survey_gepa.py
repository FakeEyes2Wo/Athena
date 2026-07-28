"""AcademicSurvey 的离线 GEPA 评测、优化与提示晋升。"""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, Self

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field, field_validator, model_validator

from athena.agents.search.academic_survey_agent import AcademicSurveyAgent
from athena.core.agent.agent import AgentContext
from athena.core.schemas import ArtifactRef, AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.research.academic_survey.cache import (
    ReplayChannelAdapter,
    SurveyCache,
)
from athena.research.academic_survey.graph import CandidateLedger, JudgmentLedger
from athena.research.academic_survey.interfaces import ChannelAdapter, SurveyChains
from athena.research.academic_survey.schemas import (
    AcademicSurveyResult,
    ChannelName,
    PromptBundle,
    SurveyCorpus,
    SurveyRequest,
)
from athena.storage.artifact_store import ArtifactStore

ReplayMode = Literal["exact_replay", "frozen_snapshot", "materialized_offline"]
PROMPT_COMPONENTS = (
    "query_understanding",
    "channel_query_rewrite",
    "relevance_judgment",
    "query_evolution",
)


class SurveyBenchmarkCase(BaseModel):
    case_id: str = Field(min_length=1)
    request: SurveyRequest
    gold_paper_ids: list[str]
    gold_titles: dict[str, str] = Field(default_factory=dict)

    @field_validator("gold_paper_ids")
    @classmethod
    def gold_ids_must_be_canonical(cls, values: list[str]) -> list[str]:
        cleaned = list(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        if any(":" not in value for value in cleaned):
            raise ValueError("gold paper IDs must use a canonical namespace")
        return cleaned


class SurveyBenchmarkSplits(BaseModel):
    feedback: list[SurveyBenchmarkCase] = Field(min_length=1)
    pareto: list[SurveyBenchmarkCase] = Field(min_length=1)
    promotion: list[SurveyBenchmarkCase] = Field(min_length=1)

    @model_validator(mode="after")
    def splits_must_be_disjoint(self) -> Self:
        case_ids = [
            [case.case_id for case in split]
            for split in (self.feedback, self.pareto, self.promotion)
        ]
        if any(len(values) != len(set(values)) for values in case_ids):
            raise ValueError("case IDs must be unique within each benchmark split")
        groups = [set(values) for values in case_ids]
        if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
            raise ValueError(
                "feedback, pareto, and promotion case IDs must be disjoint"
            )
        return self


class SurveyBenchmarkArtifacts(BaseModel):
    feedback_ref: ArtifactRef
    pareto_ref: ArtifactRef
    promotion_ref: ArtifactRef


class MaterializedSnapshotManifest(BaseModel):
    version: str
    bundle_versions: list[str]
    case_ids: list[str]
    channel_versions: dict[ChannelName, str]
    trace_refs: list[ArtifactRef] = Field(default_factory=list)


class DocumentMetrics(BaseModel):
    precision: float
    recall: float
    f1: float


class SurveyRolloutTrace(BaseModel):
    case_id: str
    bundle_version: str
    candidate_paper_ids: list[str] = Field(default_factory=list)
    predicted_paper_ids: list[str] = Field(default_factory=list)
    predicted_titles: dict[str, str] = Field(default_factory=dict)
    trace_refs: list[ArtifactRef] = Field(default_factory=list)
    judgment_differences: list[str] = Field(default_factory=list)
    first_sources: dict[str, str] = Field(default_factory=dict)
    replay_mode: ReplayMode
    snapshot_version: str = Field(min_length=1)
    invalid_reason: str | None = None


class PromotionCaseResult(BaseModel):
    case_id: str
    incumbent: DocumentMetrics
    candidate: DocumentMetrics
    incumbent_candidate_recall: float = 0
    candidate_candidate_recall: float = 0
    added_paper_ids: list[str] = Field(default_factory=list)
    removed_paper_ids: list[str] = Field(default_factory=list)


class PromotionReport(BaseModel):
    incumbent_version: str
    candidate_version: str
    incumbent_macro_f1: float
    candidate_macro_f1: float
    incumbent_macro_precision: float = 0
    candidate_macro_precision: float = 0
    incumbent_macro_candidate_recall: float = 0
    candidate_macro_candidate_recall: float = 0
    precision_preserved: bool = False
    candidate_recall_preserved: bool = False
    strictly_improved: bool
    approved: bool
    replay_mode: ReplayMode
    optimization_model_revision: str
    deployment_model_revision: str
    datasets: SurveyBenchmarkArtifacts
    snapshot_version: str
    snapshot_manifest_ref: ArtifactRef | None = None
    metric_calls: int
    elapsed_seconds: float
    per_case: list[PromotionCaseResult]


class GEPAOptimizationResult(BaseModel):
    candidate_bundle_ref: ArtifactRef
    promotion_report_ref: ArtifactRef
    active_bundle_ref: ArtifactRef
    promoted: bool


class PromptBundleRegistry(Protocol):
    async def get_active(self) -> ArtifactRef: ...

    async def activate(self, bundle_ref: ArtifactRef) -> None: ...


class MemoryPromptBundleRegistry:
    """测试和单进程离线作业使用的显式 active 指针。"""

    def __init__(self, active_ref: ArtifactRef) -> None:
        self.active_ref = active_ref

    async def get_active(self) -> ArtifactRef:
        return self.active_ref

    async def activate(self, bundle_ref: ArtifactRef) -> None:
        self.active_ref = bundle_ref


class LocalPromptBundleRegistry:
    """用原子替换持久化 active bundle 引用，不复制 bundle 内容。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    async def get_active(self) -> ArtifactRef:
        return await asyncio.to_thread(self._read)

    async def activate(self, bundle_ref: ArtifactRef) -> None:
        if not bundle_ref.strip():
            raise ValueError("bundle_ref must not be blank")
        await asyncio.to_thread(self._write, bundle_ref)

    def _read(self) -> str:
        return self.path.read_text(encoding="utf-8").strip()

    def _write(self, bundle_ref: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".prompt-bundle-", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(bundle_ref)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


async def load_active_prompt_bundle(
    artifacts: ArtifactStore, registry: PromptBundleRegistry
) -> PromptBundle:
    """解析 active 指针；在线 Agent 仍只接收普通 PromptBundle。"""
    bundle_ref = await registry.get_active()
    return PromptBundle.model_validate_json(await artifacts.get_text(bundle_ref))


class SurveyRolloutRunner(Protocol):
    def __call__(
        self,
        bundle: PromptBundle,
        case: SurveyBenchmarkCase,
        mode: ReplayMode,
    ) -> SurveyRolloutTrace: ...


class GraphSurveyRolloutRunner:
    """用生产 AcademicSurveyAgent/graph 执行离线 GEPA rollout。"""

    def __init__(
        self,
        artifacts: ArtifactStore,
        chain_factory: Callable[[PromptBundle], SurveyChains],
        channels: dict[ChannelName, ChannelAdapter],
        cache: SurveyCache,
        snapshot_channels: dict[ChannelName, ChannelAdapter] | None = None,
        snapshot_version: str | None = None,
        materialized_manifest: MaterializedSnapshotManifest | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.chain_factory = chain_factory
        self.channels = channels
        self.cache = cache
        self.snapshot_channels = snapshot_channels or {}
        self.snapshot_version = snapshot_version
        self.materialized_manifest = materialized_manifest

    def __call__(
        self,
        bundle: PromptBundle,
        case: SurveyBenchmarkCase,
        mode: ReplayMode,
    ) -> SurveyRolloutTrace:
        return asyncio.run(self._run(bundle, case, mode))

    async def _run(
        self,
        bundle: PromptBundle,
        case: SurveyBenchmarkCase,
        mode: ReplayMode,
    ) -> SurveyRolloutTrace:
        if mode == "frozen_snapshot":
            channels = self.snapshot_channels
            if not channels:
                return _invalid_trace(
                    case, bundle, mode, "no frozen snapshot configured"
                )
            if not self.snapshot_version:
                return _invalid_trace(
                    case, bundle, mode, "frozen snapshot version is required"
                )
            snapshot_version = self.snapshot_version
        else:
            channels = self._replay_channels()
            if mode == "materialized_offline":
                manifest = self.materialized_manifest
                if manifest is None:
                    return _invalid_trace(
                        case,
                        bundle,
                        mode,
                        "no materialized snapshot manifest configured",
                    )
                current_versions = {
                    name: getattr(adapter, "version", "unversioned")
                    for name, adapter in self.channels.items()
                }
                if current_versions != manifest.channel_versions:
                    return _invalid_trace(
                        case, bundle, mode, "materialized channel versions do not match"
                    )
                if (
                    bundle.version not in manifest.bundle_versions
                    or case.case_id not in manifest.case_ids
                ):
                    return _invalid_trace(
                        case, bundle, mode, "bundle/case was not materialized"
                    )
                snapshot_version = manifest.version
            else:
                if not self.snapshot_version:
                    return _invalid_trace(
                        case, bundle, mode, "exact replay snapshot version is required"
                    )
                snapshot_version = self.snapshot_version
        return await self._run_with_channels(
            bundle, case, mode, channels, snapshot_version
        )

    def _replay_channels(self) -> dict[ChannelName, ChannelAdapter]:
        return {
            name: ReplayChannelAdapter(
                name,
                getattr(adapter, "version", "unversioned"),
                self.artifacts,
                self.cache,
            )
            for name, adapter in self.channels.items()
        }

    async def _run_with_channels(
        self,
        bundle: PromptBundle,
        case: SurveyBenchmarkCase,
        mode: ReplayMode,
        channels: dict[ChannelName, ChannelAdapter],
        snapshot_version: str,
    ) -> SurveyRolloutTrace:

        request_ref = await self.artifacts.put_text(case.request.model_dump_json())
        events: list[str] = []

        async def emit(kind: str, ref: str, data: dict | None = None) -> None:
            events.append(ref)

        context = AgentContext(
            AthenaThread(
                thread_id=f"gepa:{case.case_id}:{bundle.version}",
                session_id="gepa-offline",
                status="running",
                context_ref="gepa:context",
            ),
            AthenaTurn(
                turn_id=f"rollout:{case.case_id}",
                thread_id=f"gepa:{case.case_id}:{bundle.version}",
                request_ref=request_ref,
                status="running",
            ),
            emit,
            ToolRegistry(),
            asyncio.Event(),
        )
        try:
            outcome = await AcademicSurveyAgent(
                self.artifacts,
                channels,
                chains=self.chain_factory(bundle),
                cache=self.cache,
            ).run(context)
        except Exception as error:
            return _invalid_trace(
                case,
                bundle,
                mode,
                f"{type(error).__name__}: {error}",
                snapshot_version,
            )
        result = AcademicSurveyResult.model_validate_json(
            await self.artifacts.get_text(outcome.result_ref)
        )
        if result.status == "partial":
            return _invalid_trace(
                case,
                bundle,
                mode,
                "partial survey: " + "; ".join(result.warnings),
                snapshot_version,
            )
        corpus = SurveyCorpus.model_validate_json(
            await self.artifacts.get_text(result.survey_corpus_ref)
        )
        ledger = CandidateLedger.model_validate_json(
            await self.artifacts.get_text(corpus.candidate_ledger_ref)
        )
        judgments = JudgmentLedger.model_validate_json(
            await self.artifacts.get_text(corpus.judgment_ledger_ref)
        )
        first_sources = {
            candidate.identity.paper_key(): (
                f"{candidate.observations[0].channel}/"
                f"{candidate.observations[0].query_id}"
            )
            for candidate in ledger.candidates
            if candidate.observations
        }
        judgment_differences = _judgment_differences(case, ledger, judgments)
        return SurveyRolloutTrace(
            case_id=case.case_id,
            bundle_version=bundle.version,
            candidate_paper_ids=[
                candidate.identity.paper_key() for candidate in ledger.candidates
            ],
            predicted_paper_ids=[item.paper_id for item in corpus.reference_papers],
            predicted_titles={
                item.paper_id: item.title for item in corpus.reference_papers
            },
            trace_refs=list(dict.fromkeys([outcome.result_ref, *events])),
            judgment_differences=judgment_differences,
            first_sources=first_sources,
            replay_mode=mode,
            snapshot_version=snapshot_version,
        )


async def materialize_offline_snapshot(
    bundles: list[PromptBundle],
    cases: list[SurveyBenchmarkCase],
    artifacts: ArtifactStore,
    chain_factory: Callable[[PromptBundle], SurveyChains],
    channels: dict[ChannelName, ChannelAdapter],
    cache: SurveyCache,
) -> tuple[MaterializedSnapshotManifest, ArtifactRef]:
    """独立触网采集已知 bundle/case，完成后以只读 cache manifest 冻结。"""
    if not bundles or not cases:
        raise ValueError("materialization requires at least one bundle and case")
    runner = GraphSurveyRolloutRunner(artifacts, chain_factory, channels, cache)
    traces = []
    for bundle in bundles:
        for case in cases:
            trace = await runner._run_with_channels(
                bundle,
                case,
                "materialized_offline",
                channels,
                "collecting",
            )
            if trace.invalid_reason:
                raise RuntimeError(
                    f"materialization failed for {bundle.version}/{case.case_id}: "
                    f"{trace.invalid_reason}"
                )
            traces.extend(trace.trace_refs)
    payload = {
        "bundle_versions": sorted({bundle.version for bundle in bundles}),
        "case_ids": sorted({case.case_id for case in cases}),
        "channel_versions": {
            name: getattr(adapter, "version", "unversioned")
            for name, adapter in sorted(channels.items())
        },
        "trace_refs": list(dict.fromkeys(traces)),
    }
    version = (
        "materialized-"
        + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
    )
    manifest = MaterializedSnapshotManifest(version=version, **payload)
    manifest_ref = await artifacts.put_text(manifest.model_dump_json())
    return manifest, manifest_ref


def _judgment_differences(
    case: SurveyBenchmarkCase,
    candidates: CandidateLedger,
    judgments: JudgmentLedger,
) -> list[str]:
    gold = set(case.gold_paper_ids)
    seen = set()
    differences = []
    for candidate in candidates.candidates:
        paper_id = candidate.identity.paper_key()
        seen.add(paper_id)
        record = judgments.records.get(candidate.candidate_id)
        verdict = record.judgment.verdict if record else "not_judged"
        expected = "relevant" if paper_id in gold else "not_relevant"
        if (verdict == "relevant") == (paper_id in gold):
            continue
        criteria = (
            ", ".join(
                f"{item.criterion_id}={item.verdict}"
                for item in record.judgment.criteria
            )
            if record
            else ""
        )
        differences.append(
            f"{paper_id}: expected={expected}, actual={verdict}, criteria=[{criteria}]"
        )
    differences.extend(
        f"{paper_id}: expected=relevant, actual=not_retrieved"
        for paper_id in sorted(gold - seen)
    )
    return differences


def document_metrics(predicted: list[str], gold: list[str]) -> DocumentMetrics:
    predicted_set = set(predicted)
    gold_set = set(gold)
    true_positive = len(predicted_set & gold_set)
    precision = true_positive / len(predicted_set) if predicted_set else 0.0
    recall = true_positive / len(gold_set) if gold_set else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return DocumentMetrics(precision=precision, recall=recall, f1=f1)


def build_gepa_adapter(
    seed: PromptBundle,
    runner: SurveyRolloutRunner,
    mode: ReplayMode,
):
    """延迟导入可选 GEPA 包，使在线 Agent 不依赖它。"""
    try:
        from gepa.adapters.langchain_adapter import LangChainAdapter
    except ImportError as error:
        raise RuntimeError(
            "Install the survey-optimization extra to run GEPA"
        ) from error

    def rollout(candidate: dict[str, str], example: dict) -> dict:
        case = SurveyBenchmarkCase.model_validate(example)
        bundle = _candidate_bundle(seed, candidate)
        if mode == "exact_replay" and any(
            getattr(bundle, name) != getattr(seed, name)
            for name in PROMPT_COMPONENTS
            if name != "relevance_judgment"
        ):
            trace = _invalid_trace(
                case,
                bundle,
                mode,
                "exact_replay cannot mutate query-generating prompts",
            )
            return {"trace": trace.model_dump(mode="json")}
        try:
            trace = runner(bundle, case, mode)
        except Exception as error:
            trace = _invalid_trace(
                case, bundle, mode, f"{type(error).__name__}: {error}"
            )
        return {"trace": trace.model_dump(mode="json")}

    def evaluate(example: dict, state: dict) -> tuple[float, str]:
        case = SurveyBenchmarkCase.model_validate(example)
        trace = SurveyRolloutTrace.model_validate(state["trace"])
        metrics = document_metrics(trace.predicted_paper_ids, case.gold_paper_ids)
        return (0.0 if trace.invalid_reason else metrics.f1), _feedback(
            case, trace, metrics
        )

    def reflective_record(
        example: dict, state: dict, score: float, feedback: str
    ) -> dict:
        trace = SurveyRolloutTrace.model_validate(state["trace"])
        return {
            "Inputs": {
                "query": SurveyBenchmarkCase.model_validate(example).request.topic
            },
            "Generated Outputs": trace.predicted_paper_ids,
            "Feedback": feedback,
            "trace_refs": trace.trace_refs,
            "replay_mode": trace.replay_mode,
        }

    return LangChainAdapter(
        rollout_fn=rollout,
        eval_fn=evaluate,
        reflective_record_fn=reflective_record,
        num_threads=1,
        show_progress=False,
    )


async def optimize_prompt_bundle(
    incumbent: PromptBundle,
    splits: SurveyBenchmarkSplits,
    runner: SurveyRolloutRunner,
    artifacts: ArtifactStore,
    reflection_model: BaseChatModel,
    *,
    replay_mode: ReplayMode,
    optimization_model_revision: str,
    deployment_model_revision: str,
    max_metric_calls: int = 150,
    approve: bool = False,
    registry: PromptBundleRegistry | None = None,
) -> GEPAOptimizationResult:
    """运行 GEPA，再用完全 held-out 的 promotion split 做严格晋升。"""
    try:
        from gepa import optimize
        from gepa.adapters.langchain_adapter import make_reflection_lm
    except ImportError as error:
        raise RuntimeError(
            "Install the survey-optimization extra to run GEPA"
        ) from error

    started_at = time.monotonic()
    datasets = SurveyBenchmarkArtifacts(
        feedback_ref=await artifacts.put_text(
            json.dumps([case.model_dump(mode="json") for case in splits.feedback])
        ),
        pareto_ref=await artifacts.put_text(
            json.dumps([case.model_dump(mode="json") for case in splits.pareto])
        ),
        promotion_ref=await artifacts.put_text(
            json.dumps([case.model_dump(mode="json") for case in splits.promotion])
        ),
    )
    optimization_mode: ReplayMode = (
        "frozen_snapshot" if replay_mode == "materialized_offline" else replay_mode
    )
    adapter = build_gepa_adapter(incumbent, runner, optimization_mode)
    result = await asyncio.to_thread(
        optimize,
        seed_candidate=_prompt_candidate(incumbent, replay_mode),
        trainset=[case.model_dump(mode="json") for case in splits.feedback],
        valset=[case.model_dump(mode="json") for case in splits.pareto],
        adapter=adapter,
        reflection_lm=make_reflection_lm(reflection_model),
        max_metric_calls=max_metric_calls,
    )
    candidate = _candidate_bundle(incumbent, result.best_candidate)
    snapshot_manifest_ref = None
    if replay_mode == "materialized_offline":
        if not isinstance(runner, GraphSurveyRolloutRunner):
            raise TypeError(
                "materialized_offline optimization requires GraphSurveyRolloutRunner"
            )
        manifest, snapshot_manifest_ref = await materialize_offline_snapshot(
            [incumbent, candidate],
            splits.promotion,
            artifacts,
            runner.chain_factory,
            runner.channels,
            runner.cache,
        )
        runner.materialized_manifest = manifest
    incumbent_results, incumbent_macro = await asyncio.to_thread(
        _evaluate_cases, incumbent, splits.promotion, runner, replay_mode
    )
    candidate_results, candidate_macro = await asyncio.to_thread(
        _evaluate_cases, candidate, splits.promotion, runner, replay_mode
    )
    per_case = [
        PromotionCaseResult(
            case_id=case.case_id,
            incumbent=incumbent_result[1],
            candidate=candidate_result[1],
            incumbent_candidate_recall=document_metrics(
                incumbent_result[0].candidate_paper_ids, case.gold_paper_ids
            ).recall,
            candidate_candidate_recall=document_metrics(
                candidate_result[0].candidate_paper_ids, case.gold_paper_ids
            ).recall,
            added_paper_ids=sorted(
                set(candidate_result[0].predicted_paper_ids)
                - set(incumbent_result[0].predicted_paper_ids)
            ),
            removed_paper_ids=sorted(
                set(incumbent_result[0].predicted_paper_ids)
                - set(candidate_result[0].predicted_paper_ids)
            ),
        )
        for case, incumbent_result, candidate_result in zip(
            splits.promotion, incumbent_results, candidate_results, strict=True
        )
    ]
    snapshot_versions = {
        trace.snapshot_version
        for results in (incumbent_results, candidate_results)
        for trace, _ in results
    }
    if len(snapshot_versions) != 1:
        raise ValueError("promotion rollouts must use one snapshot version")
    incumbent_precision = _macro_precision(incumbent_results)
    candidate_precision = _macro_precision(candidate_results)
    incumbent_candidate_recall = _macro_candidate_recall(
        incumbent_results, splits.promotion
    )
    candidate_candidate_recall = _macro_candidate_recall(
        candidate_results, splits.promotion
    )
    precision_preserved = candidate_precision >= incumbent_precision
    candidate_recall_preserved = (
        candidate_candidate_recall >= incumbent_candidate_recall
    )
    improved = (
        candidate_macro > incumbent_macro
        and precision_preserved
        and candidate_recall_preserved
    )
    report = PromotionReport(
        incumbent_version=incumbent.version,
        candidate_version=candidate.version,
        incumbent_macro_f1=incumbent_macro,
        candidate_macro_f1=candidate_macro,
        incumbent_macro_precision=incumbent_precision,
        candidate_macro_precision=candidate_precision,
        incumbent_macro_candidate_recall=incumbent_candidate_recall,
        candidate_macro_candidate_recall=candidate_candidate_recall,
        precision_preserved=precision_preserved,
        candidate_recall_preserved=candidate_recall_preserved,
        strictly_improved=improved,
        approved=approve and improved,
        replay_mode=replay_mode,
        optimization_model_revision=optimization_model_revision,
        deployment_model_revision=deployment_model_revision,
        datasets=datasets,
        snapshot_version=snapshot_versions.pop(),
        snapshot_manifest_ref=snapshot_manifest_ref,
        metric_calls=getattr(result, "total_metric_calls", None) or max_metric_calls,
        elapsed_seconds=time.monotonic() - started_at,
        per_case=per_case,
    )
    report_ref = await artifacts.put_text(report.model_dump_json())
    candidate = candidate.model_copy(update={"optimizer_run_ref": report_ref})
    incumbent_ref = await artifacts.put_text(incumbent.model_dump_json())
    candidate_ref = await artifacts.put_text(candidate.model_dump_json())
    promoted = improved and approve
    active_ref = candidate_ref if promoted else incumbent_ref
    if promoted and registry is not None:
        await registry.activate(candidate_ref)
    return GEPAOptimizationResult(
        candidate_bundle_ref=candidate_ref,
        promotion_report_ref=report_ref,
        active_bundle_ref=active_ref,
        promoted=promoted,
    )


def _evaluate_cases(
    bundle: PromptBundle,
    cases: list[SurveyBenchmarkCase],
    runner: SurveyRolloutRunner,
    mode: ReplayMode,
) -> tuple[list[tuple[SurveyRolloutTrace, DocumentMetrics]], float]:
    results = []
    for case in cases:
        trace = runner(bundle, case, mode)
        value = document_metrics(trace.predicted_paper_ids, case.gold_paper_ids)
        metric = (
            DocumentMetrics(precision=0, recall=0, f1=0)
            if trace.invalid_reason
            else value
        )
        results.append((trace, metric))
    macro = sum(metric.f1 for _, metric in results) / len(results)
    return results, macro


def _macro_precision(
    results: list[tuple[SurveyRolloutTrace, DocumentMetrics]],
) -> float:
    return sum(metrics.precision for _, metrics in results) / len(results)


def _macro_candidate_recall(
    results: list[tuple[SurveyRolloutTrace, DocumentMetrics]],
    cases: list[SurveyBenchmarkCase],
) -> float:
    return sum(
        document_metrics(trace.candidate_paper_ids, case.gold_paper_ids).recall
        for (trace, _), case in zip(results, cases, strict=True)
    ) / len(results)


def _prompt_candidate(
    bundle: PromptBundle, mode: ReplayMode = "frozen_snapshot"
) -> dict[str, str]:
    names = ("relevance_judgment",) if mode == "exact_replay" else PROMPT_COMPONENTS
    return {name: getattr(bundle, name) for name in names}


def _candidate_bundle(seed: PromptBundle, candidate: dict[str, str]) -> PromptBundle:
    unknown = set(candidate) - set(PROMPT_COMPONENTS)
    if unknown:
        raise ValueError(
            f"GEPA candidate cannot modify hard configuration: {sorted(unknown)}"
        )
    components = {
        name: candidate.get(name, getattr(seed, name)) for name in PROMPT_COMPONENTS
    }
    digest = hashlib.sha256(
        json.dumps(components, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12]
    return PromptBundle.model_validate(
        {
            **seed.model_dump(mode="python"),
            **components,
            "version": f"academic-survey-gepa-{digest}",
            "parent_versions": list(
                dict.fromkeys([*seed.parent_versions, seed.version])
            ),
            "optimizer_run_ref": None,
        }
    )


def _feedback(
    case: SurveyBenchmarkCase,
    trace: SurveyRolloutTrace,
    metrics: DocumentMetrics,
) -> str:
    predicted = set(trace.predicted_paper_ids)
    gold = set(case.gold_paper_ids)
    missed = sorted(gold - predicted)
    false_positive = sorted(predicted - gold)
    candidate_metrics = document_metrics(trace.candidate_paper_ids, case.gold_paper_ids)
    return "\n".join(
        [
            f"Query: {case.request.topic}",
            f"Prompt module version: {trace.bundle_version}",
            f"Execution trace refs: {trace.trace_refs}",
            f"Missed gold paper IDs + title: {[(item, case.gold_titles.get(item, '')) for item in missed]}",
            f"False-positive paper IDs + title: {[(item, trace.predicted_titles.get(item, '')) for item in false_positive]}",
            f"Per-criterion judgment differences: {trace.judgment_differences}",
            f"Channel/query first surfaced: {trace.first_sources}",
            f"Cache/snapshot mode: {trace.replay_mode}; "
            f"snapshot={trace.snapshot_version}; invalid: {trace.invalid_reason}",
            f"Candidate recall={candidate_metrics.recall:.4f}",
            f"Precision={metrics.precision:.4f} Recall={metrics.recall:.4f} F1={metrics.f1:.4f}",
        ]
    )


def _invalid_trace(
    case: SurveyBenchmarkCase,
    bundle: PromptBundle,
    mode: ReplayMode,
    reason: str,
    snapshot_version: str = "unavailable",
) -> SurveyRolloutTrace:
    return SurveyRolloutTrace(
        case_id=case.case_id,
        bundle_version=bundle.version,
        replay_mode=mode,
        snapshot_version=snapshot_version,
        invalid_reason=reason,
    )

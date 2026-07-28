"""Offline tests for AcademicSurvey GEPA replay and promotion gates."""

from types import SimpleNamespace

import pytest

from athena.optimization.academic_survey_gepa import (
    MemoryPromptBundleRegistry,
    GraphSurveyRolloutRunner,
    MaterializedSnapshotManifest,
    PromotionReport,
    SurveyBenchmarkCase,
    SurveyBenchmarkSplits,
    SurveyRolloutTrace,
    _candidate_bundle,
    _prompt_candidate,
    build_gepa_adapter,
    document_metrics,
    load_active_prompt_bundle,
    materialize_offline_snapshot,
    optimize_prompt_bundle,
)
from athena.research.academic_survey.cache import MemorySurveyCache
from athena.research.academic_survey.cache import ReplayCacheMiss
from athena.research.academic_survey.chains import DEFAULT_PROMPTS
from athena.research.academic_survey.schemas import (
    CandidateObservation,
    CriterionJudgmentDraft,
    JudgmentDraft,
    JudgmentEvidence,
    ObservedIdentity,
    QueryEvolution,
    QueryPlan,
    RelevanceCriterion,
    SearchQuery,
    SurveyRequest,
)
from athena.storage import LocalArtifactStore

gepa = pytest.importorskip("gepa")
langchain_adapter = pytest.importorskip("gepa.adapters.langchain_adapter")


def case(case_id: str) -> SurveyBenchmarkCase:
    return SurveyBenchmarkCase(
        case_id=case_id,
        request=SurveyRequest(topic=f"query {case_id}"),
        gold_paper_ids=["arxiv:2501.00001"],
        gold_titles={"arxiv:2501.00001": "Paper Agents"},
    )


def splits() -> SurveyBenchmarkSplits:
    return SurveyBenchmarkSplits(
        feedback=[case("feedback")],
        pareto=[case("pareto")],
        promotion=[case("promotion")],
    )


class MaterializeChains:
    model_revision = "fixture-model"

    def __init__(self, bundle=DEFAULT_PROMPTS):
        self.prompt_bundle_version = bundle.version

    async def understand(self, request):
        return QueryPlan(
            intent="survey",
            domain="information retrieval",
            criteria=[RelevanceCriterion(criterion_id="topic", description="topic")],
            queries=[SearchQuery(query_id="q1", text="query", channels=["arxiv"])],
        )

    async def judge(self, request, query_plan, candidate):
        return JudgmentDraft(
            criteria=[
                CriterionJudgmentDraft(
                    criterion_id="topic",
                    verdict="met",
                    evidence=[JudgmentEvidence(field="title", quote=candidate.title)],
                )
            ]
        )

    async def evolve(self, request, query_plan, accepted, searched_queries):
        return QueryEvolution()


class MaterializeChannel:
    name = "arxiv"
    version = "materialize-fixture-v1"

    def __init__(self):
        self.search_calls = 0

    async def search(self, query, constraints, limit, cancel):
        self.search_calls += 1
        return [
            CandidateObservation(
                channel="arxiv",
                query_id=query.query_id,
                query_text=query.text,
                raw_rank=1,
                identity=ObservedIdentity(arxiv_id="2501.00001"),
                title="Paper Agents",
                abstract="A retrieval paper.",
            )
        ]

    async def references(self, paper, limit, cancel):
        return []


def test_dataset_splits_must_be_disjoint() -> None:
    with pytest.raises(ValueError, match="must be disjoint"):
        SurveyBenchmarkSplits(
            feedback=[case("same")],
            pareto=[case("same")],
            promotion=[case("held-out")],
        )

    with pytest.raises(ValueError, match="unique within"):
        SurveyBenchmarkSplits(
            feedback=[case("duplicate"), case("duplicate")],
            pareto=[case("pareto")],
            promotion=[case("promotion")],
        )


def test_gold_ids_and_prompt_candidates_are_validated() -> None:
    with pytest.raises(ValueError, match="canonical namespace"):
        SurveyBenchmarkCase(
            case_id="invalid-gold",
            request=SurveyRequest(topic="query"),
            gold_paper_ids=["2501.00001"],
        )
    with pytest.raises(ValueError):
        _candidate_bundle(DEFAULT_PROMPTS, {"relevance_judgment": ""})


def test_document_f1_uses_canonical_paper_ids() -> None:
    metrics = document_metrics(
        ["arxiv:2501.00001", "doi:10.1000/false"],
        ["arxiv:2501.00001"],
    )

    assert metrics.precision == 0.5
    assert metrics.recall == 1.0
    assert metrics.f1 == pytest.approx(2 / 3)


def test_gepa_candidate_cannot_modify_hard_configuration() -> None:
    with pytest.raises(ValueError, match="cannot modify hard configuration"):
        _candidate_bundle(DEFAULT_PROMPTS, {"max_rounds": "999"})


def test_exact_replay_only_optimizes_relevance_prompt() -> None:
    assert _prompt_candidate(DEFAULT_PROMPTS, "exact_replay") == {
        "relevance_judgment": DEFAULT_PROMPTS.relevance_judgment
    }


async def test_materialized_snapshot_collects_then_replays_without_live_channel(
    tmp_path,
) -> None:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    cache = MemorySurveyCache()
    benchmark = case("materialized")
    channel = MaterializeChannel()
    manifest, manifest_ref = await materialize_offline_snapshot(
        [DEFAULT_PROMPTS],
        [benchmark],
        artifacts,
        MaterializeChains,
        {"arxiv": channel},
        cache,
    )
    collection_calls = channel.search_calls
    stored = MaterializedSnapshotManifest.model_validate_json(
        await artifacts.get_text(manifest_ref)
    )
    runner = GraphSurveyRolloutRunner(
        artifacts,
        MaterializeChains,
        {"arxiv": channel},
        cache,
        materialized_manifest=manifest,
    )

    trace = await runner._run(DEFAULT_PROMPTS, benchmark, "materialized_offline")

    assert stored == manifest
    assert trace.invalid_reason is None
    assert trace.snapshot_version == manifest.version
    assert trace.candidate_paper_ids == ["arxiv:2501.00001"]
    assert trace.predicted_paper_ids == ["arxiv:2501.00001"]
    assert channel.search_calls == collection_calls


async def test_materialized_optimization_freezes_finalists_before_promotion(
    tmp_path, monkeypatch
) -> None:
    def fake_optimize(**kwargs):
        candidate = dict(kwargs["seed_candidate"])
        candidate["relevance_judgment"] = "candidate judge"
        return SimpleNamespace(best_candidate=candidate, total_metric_calls=1)

    monkeypatch.setattr(gepa, "optimize", fake_optimize)
    monkeypatch.setattr(langchain_adapter, "make_reflection_lm", lambda model: None)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    live_channel = MaterializeChannel()
    runner = GraphSurveyRolloutRunner(
        artifacts,
        MaterializeChains,
        {"arxiv": live_channel},
        MemorySurveyCache(),
        snapshot_channels={"arxiv": MaterializeChannel()},
        snapshot_version="frozen-v1",
    )

    result = await optimize_prompt_bundle(
        DEFAULT_PROMPTS,
        splits(),
        runner,
        artifacts,
        object(),
        replay_mode="materialized_offline",
        optimization_model_revision="optimizer-model-v1",
        deployment_model_revision="deployment-model-v1",
        max_metric_calls=4,
    )
    report = PromotionReport.model_validate_json(
        await artifacts.get_text(result.promotion_report_ref)
    )

    assert report.snapshot_manifest_ref is not None
    assert report.snapshot_version.startswith("materialized-")
    assert not result.promoted


def test_exact_replay_miss_invalidates_rollout_instead_of_scoring_empty() -> None:
    def missing(bundle, benchmark, mode):
        raise ReplayCacheMiss("new query")

    adapter = build_gepa_adapter(DEFAULT_PROMPTS, missing, "exact_replay")
    result = adapter.evaluate(
        [case("feedback").model_dump(mode="json")],
        {
            "query_understanding": DEFAULT_PROMPTS.query_understanding,
            "channel_query_rewrite": DEFAULT_PROMPTS.channel_query_rewrite,
            "relevance_judgment": DEFAULT_PROMPTS.relevance_judgment,
            "query_evolution": DEFAULT_PROMPTS.query_evolution,
        },
        capture_traces=True,
    )

    assert result.scores == [0.0]
    assert "ReplayCacheMiss" in result.trajectories[0]["feedback"]
    assert "invalid" in result.trajectories[0]["feedback"]


def test_exact_replay_rejects_query_prompt_mutation() -> None:
    def should_not_run(bundle, benchmark, mode):
        raise AssertionError("query-mutating exact replay reached the graph")

    adapter = build_gepa_adapter(DEFAULT_PROMPTS, should_not_run, "exact_replay")
    candidate = {
        "query_understanding": "mutated query planner",
        "channel_query_rewrite": DEFAULT_PROMPTS.channel_query_rewrite,
        "relevance_judgment": DEFAULT_PROMPTS.relevance_judgment,
        "query_evolution": DEFAULT_PROMPTS.query_evolution,
    }

    result = adapter.evaluate(
        [case("feedback").model_dump(mode="json")],
        candidate,
        capture_traces=True,
    )

    assert result.scores == [0.0]
    assert "cannot mutate" in result.trajectories[0]["feedback"]


async def test_promotion_requires_strict_held_out_improvement(
    tmp_path, monkeypatch
) -> None:
    def fake_optimize(**kwargs):
        candidate = dict(kwargs["seed_candidate"])
        candidate["relevance_judgment"] = "improved judge"
        return SimpleNamespace(best_candidate=candidate)

    monkeypatch.setattr(gepa, "optimize", fake_optimize)
    monkeypatch.setattr(langchain_adapter, "make_reflection_lm", lambda model: None)

    def runner(bundle, benchmark, mode):
        predicted = (
            ["arxiv:2501.00001"]
            if bundle.relevance_judgment == "improved judge"
            else []
        )
        return SurveyRolloutTrace(
            case_id=benchmark.case_id,
            bundle_version=bundle.version,
            predicted_paper_ids=predicted,
            replay_mode=mode,
            snapshot_version="snapshot-v1",
        )

    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    incumbent_ref = await artifacts.put_text(DEFAULT_PROMPTS.model_dump_json())
    registry = MemoryPromptBundleRegistry(incumbent_ref)
    result = await optimize_prompt_bundle(
        DEFAULT_PROMPTS,
        splits(),
        runner,
        artifacts,
        object(),
        replay_mode="frozen_snapshot",
        optimization_model_revision="optimizer-model-v1",
        deployment_model_revision="deployment-model-v1",
        max_metric_calls=4,
        approve=True,
        registry=registry,
    )

    assert result.promoted
    assert result.active_bundle_ref == result.candidate_bundle_ref
    assert registry.active_ref == result.candidate_bundle_ref
    active = await load_active_prompt_bundle(artifacts, registry)
    report = PromotionReport.model_validate_json(
        await artifacts.get_text(result.promotion_report_ref)
    )
    assert active.relevance_judgment == "improved judge"
    assert report.snapshot_version == "snapshot-v1"
    assert (
        len(
            {
                report.datasets.feedback_ref,
                report.datasets.pareto_ref,
                report.datasets.promotion_ref,
            }
        )
        == 3
    )
    assert report.per_case[0].added_paper_ids == ["arxiv:2501.00001"]


async def test_equal_promotion_f1_never_promotes(tmp_path, monkeypatch) -> None:
    def fake_optimize(**kwargs):
        return SimpleNamespace(best_candidate=dict(kwargs["seed_candidate"]))

    monkeypatch.setattr(gepa, "optimize", fake_optimize)
    monkeypatch.setattr(langchain_adapter, "make_reflection_lm", lambda model: None)

    def runner(bundle, benchmark, mode):
        return SurveyRolloutTrace(
            case_id=benchmark.case_id,
            bundle_version=bundle.version,
            predicted_paper_ids=["arxiv:2501.00001"],
            replay_mode=mode,
            snapshot_version="snapshot-v1",
        )

    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    result = await optimize_prompt_bundle(
        DEFAULT_PROMPTS,
        splits(),
        runner,
        artifacts,
        object(),
        replay_mode="frozen_snapshot",
        optimization_model_revision="optimizer-model-v1",
        deployment_model_revision="deployment-model-v1",
        max_metric_calls=4,
        approve=True,
    )

    assert not result.promoted
    assert result.active_bundle_ref != result.candidate_bundle_ref


@pytest.mark.parametrize(
    (
        "incumbent_predicted",
        "candidate_predicted",
        "incumbent_candidates",
        "candidate_candidates",
        "precision_preserved",
        "candidate_recall_preserved",
    ),
    [
        (
            ["arxiv:2501.00001"],
            ["arxiv:2501.00001", "arxiv:2501.00002", "doi:10.1000/false"],
            ["arxiv:2501.00001", "arxiv:2501.00002"],
            ["arxiv:2501.00001", "arxiv:2501.00002", "doi:10.1000/false"],
            False,
            True,
        ),
        (
            [],
            ["arxiv:2501.00001"],
            ["arxiv:2501.00001", "arxiv:2501.00002"],
            ["arxiv:2501.00001"],
            True,
            False,
        ),
    ],
)
async def test_promotion_blocks_precision_or_candidate_recall_regression(
    tmp_path,
    monkeypatch,
    incumbent_predicted,
    candidate_predicted,
    incumbent_candidates,
    candidate_candidates,
    precision_preserved,
    candidate_recall_preserved,
) -> None:
    def fake_optimize(**kwargs):
        candidate = dict(kwargs["seed_candidate"])
        candidate["relevance_judgment"] = "improved judge"
        return SimpleNamespace(best_candidate=candidate)

    monkeypatch.setattr(gepa, "optimize", fake_optimize)
    monkeypatch.setattr(langchain_adapter, "make_reflection_lm", lambda model: None)

    def benchmark(case_id):
        return SurveyBenchmarkCase(
            case_id=case_id,
            request=SurveyRequest(topic=f"query {case_id}"),
            gold_paper_ids=["arxiv:2501.00001", "arxiv:2501.00002"],
        )

    benchmark_splits = SurveyBenchmarkSplits(
        feedback=[benchmark("feedback-gated")],
        pareto=[benchmark("pareto-gated")],
        promotion=[benchmark("promotion-gated")],
    )

    def runner(bundle, benchmark_case, mode):
        is_candidate = bundle.relevance_judgment == "improved judge"
        return SurveyRolloutTrace(
            case_id=benchmark_case.case_id,
            bundle_version=bundle.version,
            candidate_paper_ids=(
                candidate_candidates if is_candidate else incumbent_candidates
            ),
            predicted_paper_ids=(
                candidate_predicted if is_candidate else incumbent_predicted
            ),
            replay_mode=mode,
            snapshot_version="snapshot-v1",
        )

    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    result = await optimize_prompt_bundle(
        DEFAULT_PROMPTS,
        benchmark_splits,
        runner,
        artifacts,
        object(),
        replay_mode="frozen_snapshot",
        optimization_model_revision="optimizer-model-v1",
        deployment_model_revision="deployment-model-v1",
        max_metric_calls=4,
        approve=True,
    )
    report = PromotionReport.model_validate_json(
        await artifacts.get_text(result.promotion_report_ref)
    )

    assert report.candidate_macro_f1 > report.incumbent_macro_f1
    assert report.precision_preserved is precision_preserved
    assert report.candidate_recall_preserved is candidate_recall_preserved
    assert not result.promoted

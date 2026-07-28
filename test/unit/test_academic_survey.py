"""Offline tests for the AcademicSurvey SPAR graph and handoff."""

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from athena.agents.search.academic_survey_agent import AcademicSurveyAgent
from athena.core.agent.agent import AgentContext
from athena.core.schemas import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.research.academic_survey.logic import (
    constraint_status,
    enforce_plan_policy,
    merge_observations,
    possible_duplicate_pairs,
    rank_candidates,
    satisfies_constraints,
    validate_judgment,
)
from athena.research.academic_survey import budget as survey_budget
from athena.research.academic_survey.schemas import (
    AcademicSurveyResult,
    CandidateObservation,
    CriterionJudgmentDraft,
    JudgmentDraft,
    JudgmentEvidence,
    ObservedIdentity,
    QueryEvolution,
    QueryPlan,
    RankingIntent,
    RelevanceCriterion,
    SearchPage,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
    SurveyCorpus,
    SurveyRequest,
    SurveyStats,
)
from athena.research.paper_source.schemas import PaperSourceRequest
from athena.storage import LocalArtifactStore


def observation(
    title: str,
    *,
    arxiv_id: str | None = None,
    doi: str | None = None,
    channel: str = "arxiv",
    query_id: str = "q1",
    rank: int = 1,
    year: int | None = 2025,
) -> CandidateObservation:
    return CandidateObservation(
        channel=channel,
        query_id=query_id,
        query_text="paper search",
        raw_rank=rank,
        identity=ObservedIdentity(arxiv_id=arxiv_id, doi=doi, title=title),
        title=title,
        abstract=f"{title} presents a verified retrieval method.",
        year=year,
        language="en",
    )


def plan(channels: list[str] | None = None) -> QueryPlan:
    return QueryPlan(
        intent="survey",
        domain="information retrieval",
        criteria=[
            RelevanceCriterion(
                criterion_id="topic", description="Is about the requested topic"
            )
        ],
        queries=[
            SearchQuery(
                query_id="q1",
                text="paper search",
                channels=channels or ["arxiv"],
            )
        ],
        ranking_intent=RankingIntent(),
    )


class FakeChains:
    prompt_bundle_version = "test-bundle-v1"

    def __init__(
        self,
        query_plan: QueryPlan,
        evolved: list[SearchQuery] | None = None,
    ) -> None:
        self.query_plan = query_plan
        self.evolved = evolved or []
        self.judged: list[str] = []

    async def understand(self, request: SurveyRequest) -> QueryPlan:
        return self.query_plan

    async def judge(
        self,
        request: SurveyRequest,
        query_plan: QueryPlan,
        candidate: SurveyCandidate,
    ) -> JudgmentDraft:
        self.judged.append(candidate.candidate_id)
        verdict = "unmet" if candidate.title.startswith("Irrelevant") else "met"
        evidence = (
            []
            if verdict == "unmet"
            else [JudgmentEvidence(field="title", quote=candidate.title)]
        )
        return JudgmentDraft(
            criteria=[
                CriterionJudgmentDraft(
                    criterion_id="topic", verdict=verdict, evidence=evidence
                )
            ]
        )

    async def evolve(
        self,
        request: SurveyRequest,
        query_plan: QueryPlan,
        accepted: list[SurveyCandidate],
        searched_queries: list[str],
    ) -> QueryEvolution:
        return QueryEvolution(
            queries=[
                query for query in self.evolved if query.text not in searched_queries
            ]
        )


class FakeChannel:
    name = "arxiv"

    def __init__(
        self,
        searches: dict[str, list[CandidateObservation]] | None = None,
        references: dict[str, list[CandidateObservation]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.searches = searches or {}
        self.reference_results = references or {}
        self.error = error
        self.search_calls = 0
        self.searched_texts: list[str] = []
        self.reference_calls: list[str] = []

    async def search(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        self.search_calls += 1
        self.searched_texts.append(query.text)
        if self.error:
            raise self.error
        return self.searches.get(query.text, [])[:limit]

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        self.reference_calls.append(paper.candidate_id)
        return self.reference_results.get(paper.candidate_id, [])[:limit]


class PagedFakeChannel(FakeChannel):
    async def search_page(self, query, constraints, limit, cursor, cancel):
        self.search_calls += 1
        self.searched_texts.append(query.text)
        page = 1 if cursor is None else 2
        return SearchPage(
            observations=[
                observation(
                    f"Relevant page {page}",
                    arxiv_id=f"2501.{page:05d}",
                    query_id=query.query_id,
                )
            ],
            next_cursor="page-2" if cursor is None else None,
        )


async def run_agent(tmp_path, chains, channels, *, cancel=None):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    request_ref = await artifacts.put_text(
        SurveyRequest(topic="academic paper search").model_dump_json()
    )
    events = []

    async def emit(kind, ref, data=None):
        events.append((kind, ref, data))

    context = AgentContext(
        thread=AthenaThread(
            thread_id="thread-1",
            session_id="session-1",
            status="running",
            context_ref="context:unchanged",
        ),
        turn=AthenaTurn(
            turn_id="turn-1",
            thread_id="thread-1",
            request_ref=request_ref,
            status="running",
        ),
        emit=emit,
        tools=ToolRegistry(),
        cancel=cancel or asyncio.Event(),
    )
    outcome = await AcademicSurveyAgent(artifacts, channels, chains=chains).run(context)
    result = AcademicSurveyResult.model_validate_json(
        await artifacts.get_text(outcome.result_ref)
    )
    corpus = SurveyCorpus.model_validate_json(
        await artifacts.get_text(result.survey_corpus_ref)
    )
    return artifacts, outcome, result, corpus, events


def test_identity_merge_is_transitive_and_never_uses_title() -> None:
    same_title = "A shared title"
    observations = [
        observation(same_title, arxiv_id="2501.00001"),
        observation(
            same_title,
            arxiv_id="2501.00001",
            doi="10.1000/bridge",
            channel="openalex",
        ),
        observation(same_title, doi="10.1000/bridge", channel="semantic_scholar"),
        observation(same_title, arxiv_id="2501.00002"),
        observation("Title only", year=None),
    ]

    candidates, quarantined = merge_observations(observations)

    assert len(candidates) == 2
    assert len(candidates[0].observations) == 3
    assert candidates[0].identity.doi == "10.1000/bridge"
    assert [item.title for item in quarantined] == ["Title only"]
    assert possible_duplicate_pairs(candidates) == [
        ("arxiv:2501.00001", "arxiv:2501.00002")
    ]


def test_hard_constraints_reject_missing_metadata() -> None:
    candidate = merge_observations(
        [observation("Relevant paper", arxiv_id="2501.00001", year=None)]
    )[0][0]

    assert not satisfies_constraints(
        candidate, SurveyConstraints(year_from=2020, languages=["en"])
    )
    assert (
        constraint_status(
            candidate, SurveyConstraints(year_from=2020, languages=["en"])
        )
        == "unknown"
    )


def test_invalid_evidence_cannot_produce_relevant_verdict() -> None:
    candidate = merge_observations(
        [observation("Relevant paper", arxiv_id="2501.00001")]
    )[0][0]
    draft = JudgmentDraft(
        criteria=[
            CriterionJudgmentDraft(
                criterion_id="topic",
                verdict="met",
                evidence=[JudgmentEvidence(field="abstract", quote="invented quote")],
            )
        ]
    )

    judgment = validate_judgment(plan(), candidate, draft, "test-bundle-v1")

    assert judgment.verdict == "uncertain"
    assert judgment.criteria[0].verdict == "unknown"


def test_unmet_without_counterevidence_is_uncertain() -> None:
    candidate = merge_observations(
        [observation("Relevant paper", arxiv_id="2501.00001")]
    )[0][0]
    judgment = validate_judgment(
        plan(),
        candidate,
        JudgmentDraft(
            criteria=[CriterionJudgmentDraft(criterion_id="topic", verdict="unmet")]
        ),
        "test-bundle-v1",
    )

    assert judgment.verdict == "uncertain"


def test_non_biomedical_plan_drops_pubmed_unless_explicitly_requested() -> None:
    query_plan = plan(["arxiv", "pubmed"])
    query_plan.domain = "particle physics"

    automatic = enforce_plan_policy(
        query_plan, SurveyRequest(topic="particle detector calibration")
    )
    explicit = enforce_plan_policy(
        query_plan, SurveyRequest(topic="search PubMed for detector medicine")
    )

    assert automatic.queries[0].channels == ["arxiv"]
    assert explicit.queries[0].channels == ["arxiv", "pubmed"]


def test_ranking_is_deterministic_on_ties() -> None:
    candidates, _ = merge_observations(
        [
            observation("Paper B", arxiv_id="2501.00002"),
            observation("Paper A", arxiv_id="2501.00001"),
        ]
    )
    judgments = {
        candidate.candidate_id: validate_judgment(
            plan(),
            candidate,
            JudgmentDraft(
                criteria=[
                    CriterionJudgmentDraft(
                        criterion_id="topic",
                        verdict="met",
                        evidence=[
                            JudgmentEvidence(field="title", quote=candidate.title)
                        ],
                    )
                ]
            ),
            "test-bundle-v1",
        )
        for candidate in candidates
    }

    ranked = rank_candidates(candidates, judgments, plan(), current_year=2026)

    assert [item.candidate.candidate_id for item in ranked] == [
        "arxiv:2501.00001",
        "arxiv:2501.00002",
    ]
    assert ranked[0].breakdown.formula_version == "academic-survey-ranking-v1"
    assert ranked[0].breakdown.raw_rrf > 0


async def test_agent_runs_two_rounds_refchain_and_relevant_only_handoff(
    tmp_path,
) -> None:
    root = observation("Relevant root", arxiv_id="2501.00001")
    irrelevant = observation("Irrelevant result", arxiv_id="2501.00002", rank=2)
    title_only = observation("Title only", year=None, rank=3)
    reference = observation("Relevant reference", doi="10.1000/reference")
    evolved = SearchQuery(
        query_id="q2", text="paper search limitations", channels=["arxiv"]
    )
    chains = FakeChains(plan(), [evolved])
    channel = FakeChannel(
        searches={
            "paper search": [root, irrelevant, title_only],
            "paper search limitations": [root],
        },
        references={"arxiv:2501.00001": [reference]},
    )

    artifacts, outcome, result, corpus, events = await run_agent(
        tmp_path, chains, {"arxiv": channel}
    )

    assert outcome.next_context_ref == "context:unchanged"
    assert result.status == "complete"
    assert corpus.status == "complete"
    assert corpus.reference_papers
    assert {paper.title for paper in corpus.reference_papers} == {
        "Relevant root",
        "Relevant reference",
    }
    source_request = PaperSourceRequest.model_validate_json(
        await artifacts.get_text(result.paper_source_request_ref)
    )
    stats = SurveyStats.model_validate_json(await artifacts.get_text(result.stats_ref))
    assert [paper.identity.paper_key() for paper in source_request.papers] == [
        paper.paper_id for paper in corpus.reference_papers
    ]
    assert source_request.corpus_ref == result.survey_corpus_ref
    assert stats.channel_rewards == {"arxiv": 1}
    assert len(stats.batch_allocations) == 2
    assert channel.reference_calls == ["arxiv:2501.00001"]
    assert events[-1][0] == "academic_survey/completed"


async def test_single_round_covers_all_initial_query_families(
    tmp_path, monkeypatch
) -> None:
    channels = ["arxiv", "openalex", "semantic_scholar", "pubmed"]
    query_plan = plan(channels).model_copy(
        update={
            "queries": [
                SearchQuery(
                    query_id=f"q{index}", text=f"family {index}", channels=channels
                )
                for index in range(1, 4)
            ]
        }
    )
    searches = {
        f"family {index}": [
            observation(f"Relevant family {index}", arxiv_id=f"2501.{index:05d}")
        ]
        for index in range(1, 4)
    }
    adapters = {name: FakeChannel(searches=searches) for name in channels}
    monkeypatch.setitem(
        survey_budget._BUDGETS,
        "fast",
        replace(survey_budget.budget_for("fast"), max_rounds=1),
    )

    artifacts, _, result, corpus, _ = await run_agent(
        tmp_path, FakeChains(query_plan), adapters
    )
    stats = SurveyStats.model_validate_json(await artifacts.get_text(result.stats_ref))

    assert stats.planned_queries == 3
    assert stats.executed_queries == 3
    assert stats.query_coverage == 1.0
    assert {batch["query_id"] for batch in stats.query_channel_batches} == {
        "q1",
        "q2",
        "q3",
    }
    assert len(corpus.reference_papers) == 3


async def test_evolution_appends_without_dropping_unexecuted_queries(
    tmp_path, monkeypatch
) -> None:
    query_plan = plan().model_copy(
        update={
            "queries": [
                SearchQuery(query_id="q1", text="family one", channels=["arxiv"]),
                SearchQuery(query_id="q2", text="family two", channels=["arxiv"]),
            ]
        }
    )
    evolved = SearchQuery(query_id="q3", text="family three", channels=["arxiv"])
    channel = FakeChannel(
        searches={
            "family one": [observation("Relevant one", arxiv_id="2501.00001")],
            "family two": [observation("Relevant two", arxiv_id="2501.00002")],
            "family three": [observation("Relevant three", arxiv_id="2501.00003")],
        }
    )
    monkeypatch.setitem(
        survey_budget._BUDGETS,
        "fast",
        replace(
            survey_budget.budget_for("fast"),
            max_rounds=2,
            max_batches_per_round=1,
        ),
    )

    artifacts, _, result, _, _ = await run_agent(
        tmp_path, FakeChains(query_plan, [evolved]), {"arxiv": channel}
    )
    stats = SurveyStats.model_validate_json(await artifacts.get_text(result.stats_ref))

    assert channel.searched_texts == ["family one", "family two"]
    assert stats.query_coverage == 1.0


async def test_pagination_continues_after_handoff_cap(tmp_path, monkeypatch) -> None:
    channel = PagedFakeChannel()
    monkeypatch.setitem(
        survey_budget._BUDGETS,
        "fast",
        replace(
            survey_budget.budget_for("fast"),
            max_rounds=2,
            max_batches_per_round=1,
            max_final_papers=1,
        ),
    )

    artifacts, _, result, corpus, _ = await run_agent(
        tmp_path, FakeChains(plan()), {"arxiv": channel}
    )
    stats = SurveyStats.model_validate_json(await artifacts.get_text(result.stats_ref))

    assert channel.search_calls == 2
    assert stats.paginated_pulls == 1
    assert stats.unique_candidates == 2
    assert stats.stop_reason == "max_rounds"
    assert len(corpus.reference_papers) == 1


async def test_zero_results_are_complete_without_paper_source_request(tmp_path) -> None:
    _, _, result, corpus, _ = await run_agent(
        tmp_path, FakeChains(plan()), {"arxiv": FakeChannel()}
    )

    assert result.status == "complete"
    assert result.paper_source_request_ref is None
    assert corpus.reference_papers == []


async def test_title_only_result_is_enriched_once_by_exact_title(tmp_path) -> None:
    title_only = observation("Relevant enriched", year=None)
    enriched = observation(
        "Relevant enriched",
        doi="10.1000/enriched",
        channel="openalex",
        year=2025,
    )
    arxiv = FakeChannel(searches={"paper search": [title_only]})
    openalex = FakeChannel(searches={"Relevant enriched": [enriched]})

    _, _, result, corpus, _ = await run_agent(
        tmp_path,
        FakeChains(plan()),
        {"arxiv": arxiv, "openalex": openalex},
    )

    assert result.paper_source_request_ref is not None
    assert [item.paper_id for item in corpus.reference_papers] == [
        "doi:10.1000/enriched"
    ]
    assert openalex.search_calls == 1


async def test_one_channel_failure_returns_partial_result(tmp_path) -> None:
    query_plan = plan(["arxiv", "semantic_scholar"])
    root = observation("Relevant root", arxiv_id="2501.00001")
    _, _, result, corpus, _ = await run_agent(
        tmp_path,
        FakeChains(query_plan),
        {
            "arxiv": FakeChannel(searches={"paper search": [root]}),
            "semantic_scholar": FakeChannel(error=RuntimeError("offline")),
        },
    )

    assert result.status == "partial"
    assert corpus.status == "partial"
    assert len(corpus.reference_papers) == 1
    assert any("offline" in warning for warning in result.warnings)


async def test_preexisting_cancellation_propagates(tmp_path) -> None:
    cancel = asyncio.Event()
    cancel.set()

    with pytest.raises(asyncio.CancelledError):
        await run_agent(
            tmp_path,
            FakeChains(plan()),
            {"arxiv": FakeChannel()},
            cancel=cancel,
        )


async def test_hard_deadline_returns_partial_corpus(tmp_path, monkeypatch) -> None:
    class SlowChannel(FakeChannel):
        async def search(self, query, constraints, limit, cancel):
            await asyncio.sleep(0.5)
            return []

    monkeypatch.setitem(
        survey_budget._BUDGETS,
        "fast",
        replace(survey_budget.budget_for("fast"), max_seconds=0.2),
    )

    artifacts, _, result, corpus, _ = await run_agent(
        tmp_path, FakeChains(plan()), {"arxiv": SlowChannel()}
    )
    stats = SurveyStats.model_validate_json(await artifacts.get_text(result.stats_ref))

    assert result.status == "partial"
    assert corpus.status == "partial"
    assert stats.stop_reason == "max_seconds"


async def test_structured_judgment_failure_after_retry_fails_node(tmp_path) -> None:
    class BrokenJudge(FakeChains):
        async def judge(self, request, query_plan, candidate):
            raise ValueError("invalid structured judgment")

    root = observation("Relevant root", arxiv_id="2501.00001")
    with pytest.raises(ValueError, match="invalid structured judgment"):
        await run_agent(
            tmp_path,
            BrokenJudge(plan()),
            {"arxiv": FakeChannel(searches={"paper search": [root]})},
        )


async def test_stats_are_scoped_to_each_agent_run(tmp_path) -> None:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    request_ref = await artifacts.put_text(
        SurveyRequest(topic="metrics survey").model_dump_json()
    )
    agent = AcademicSurveyAgent(
        artifacts,
        {"arxiv": FakeChannel()},
        chains=FakeChains(plan()),
    )

    async def emit(kind, ref, data=None):
        return None

    async def execute(turn_id: str) -> SurveyStats:
        outcome = await agent.run(
            AgentContext(
                AthenaThread(
                    thread_id=f"thread-{turn_id}",
                    session_id="session",
                    status="running",
                    context_ref="context",
                ),
                AthenaTurn(
                    turn_id=turn_id,
                    thread_id=f"thread-{turn_id}",
                    request_ref=request_ref,
                    status="running",
                ),
                emit,
                ToolRegistry(),
                asyncio.Event(),
            )
        )
        result = AcademicSurveyResult.model_validate_json(
            await artifacts.get_text(outcome.result_ref)
        )
        return SurveyStats.model_validate_json(
            await artifacts.get_text(result.stats_ref)
        )

    first = await execute("first")
    second = await execute("second")

    assert first.llm_calls == 2
    assert first.cache_misses == 1
    assert second.llm_calls == 0
    assert second.cache_misses == 0
    assert second.cache_hits == 3


async def test_checkpoint_state_keeps_large_candidate_data_behind_refs(
    tmp_path,
) -> None:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    request_ref = await artifacts.put_text(
        SurveyRequest(topic="checkpoint survey").model_dump_json()
    )
    channel = FakeChannel(
        searches={"paper search": [observation("Relevant root", arxiv_id="2501.00001")]}
    )
    saver = InMemorySaver()
    agent = AcademicSurveyAgent(
        artifacts,
        {"arxiv": channel},
        chains=FakeChains(plan()),
        checkpointer=saver,
    )

    async def emit(kind, ref, data=None):
        pass

    context = AgentContext(
        AthenaThread(
            thread_id="checkpoint-thread",
            session_id="session",
            status="running",
            context_ref="context:unchanged",
        ),
        AthenaTurn(
            turn_id="checkpoint-turn",
            thread_id="checkpoint-thread",
            request_ref=request_ref,
            status="running",
        ),
        emit,
        ToolRegistry(),
        asyncio.Event(),
    )
    await agent.run(context)
    await agent.run(context)

    checkpoint_id = hashlib.sha256(b"checkpoint-thread:checkpoint-turn").hexdigest()
    checkpoint = await saver.aget_tuple({"configurable": {"thread_id": checkpoint_id}})
    serialized = json.dumps(checkpoint.checkpoint["channel_values"])
    assert "A retrieval agent" not in serialized
    assert "Relevant root" not in serialized
    assert channel.search_calls == 1

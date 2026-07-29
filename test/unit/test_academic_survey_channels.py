"""Offline fixtures for AcademicSurvey channels, cache, and bandit."""

import asyncio
import json
import urllib.parse
from datetime import date

import pytest

from athena.research.academic_survey.budget import (
    allocate_search_indices,
    budget_for,
    ucb_score,
)
from athena.research.academic_survey.cache import (
    CachedChannelAdapter,
    MemorySurveyCache,
    ReplayCacheMiss,
    ReplayChannelAdapter,
)
from athena.research.academic_survey.channels import (
    ArxivChannel,
    OpenAlexChannel,
    PubMedChannel,
    SemanticScholarChannel,
)
from athena.research.academic_survey.logic import merge_observations
from athena.research.academic_survey.schemas import (
    CandidateObservation,
    ObservedIdentity,
    SearchPage,
    SearchQuery,
    SurveyConstraints,
)
from athena.research.paper_source.http import HostRateLimiter, HttpResponse
from athena.storage import LocalArtifactStore


class FakeTransport:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        self.calls.append(url)
        for marker, body in self.routes.items():
            if marker in url:
                return HttpResponse(200, url, body, {})
        return HttpResponse(404, url, b"missing", {})


def limiter(transport: FakeTransport) -> HostRateLimiter:
    return HostRateLimiter(
        transport,
        default_interval=0,
        bucket_intervals={
            "arxiv.org": 0,
            "openalex.org": 0,
            "api.semanticscholar.org": 0,
            "eutils.ncbi.nlm.nih.gov": 0,
        },
        max_retries=0,
    )


def query(channel: str) -> SearchQuery:
    return SearchQuery(query_id="q1", text="paper agents", channels=[channel])


ARXIV = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry><id>https://arxiv.org/abs/2501.00001v2</id>
    <published>2025-01-02T00:00:00Z</published><updated>2025-01-03T00:00:00Z</updated>
    <title>Paper Agents</title><summary>A retrieval agent.</summary>
    <author><name>Ada Author</name></author><category term="cs.IR"/>
    <arxiv:doi>10.1000/agents</arxiv:doi><arxiv:journal_ref>IR Journal</arxiv:journal_ref>
  </entry>
</feed>"""

OPENALEX = {
    "meta": {"next_cursor": "next-openalex"},
    "results": [
        {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1000/agents",
            "display_name": "Paper Agents",
            "publication_year": 2025,
            "publication_date": "2025-01-02",
            "language": "en",
            "cited_by_count": 9,
            "authorships": [{"author": {"display_name": "Ada Author"}}],
            "primary_location": {"source": {"display_name": "IR Journal"}},
            "best_oa_location": {
                "pdf_url": "https://example.test/paper.pdf",
                "license": "cc-by",
            },
            "abstract_inverted_index": {"Paper": [0], "agents": [1]},
        }
    ],
}

S2 = {
    "next": 5,
    "data": [
        {
            "paperId": "s2-1",
            "externalIds": {"ArXiv": "2501.00001", "DOI": "10.1000/agents"},
            "title": "Paper Agents",
            "abstract": "A retrieval agent.",
            "authors": [{"name": "Ada Author"}],
            "year": 2025,
            "publicationDate": "2025-01-02",
            "venue": "IR Journal",
            "citationCount": 9,
            "openAccessPdf": {"url": "https://example.test/paper.pdf"},
        }
    ],
}

PUBMED_XML = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation>
<PMID>42</PMID><Article><ArticleTitle>Paper Agents</ArticleTitle>
<Abstract><AbstractText>A retrieval agent.</AbstractText></Abstract>
<AuthorList><Author><ForeName>Ada</ForeName><LastName>Author</LastName></Author></AuthorList>
<Journal><Title>IR Journal</Title><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue></Journal>
<Language>eng</Language></Article><ReferenceList><Reference><ArticleIdList>
<ArticleId IdType="doi">10.1000/cited-paper</ArticleId>
<ArticleId IdType="pmc">PMC-CITED</ArticleId></ArticleIdList></Reference>
</ReferenceList></MedlineCitation><PubmedData><ArticleIdList>
<ArticleId IdType="pubmed">42</ArticleId><ArticleId IdType="doi">10.1000/agents</ArticleId>
<ArticleId IdType="pmc">PMC42</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


async def test_arxiv_channel_parses_atom_and_persists_raw_response(tmp_path) -> None:
    transport = FakeTransport({"api/query": ARXIV})
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    channel = ArxivChannel(limiter(transport), artifacts)

    result = await channel.search(
        query("arxiv"), SurveyConstraints(year_from=2020), 5, asyncio.Event()
    )

    assert result[0].identity.arxiv_id == "2501.00001"
    assert result[0].identity.doi == "10.1000/agents"
    assert result[0].raw_response_ref
    assert await artifacts.get_bytes(result[0].raw_response_ref) == ARXIV


async def test_arxiv_channel_preserves_fielded_query(tmp_path) -> None:
    transport = FakeTransport({"api/query": ARXIV})
    channel = ArxivChannel(limiter(transport), LocalArtifactStore(tmp_path))
    fielded = SearchQuery(
        query_id="q1",
        text='all:"paper agents" AND abs:retrieval',
        channels=["arxiv"],
    )

    await channel.search(fielded, SurveyConstraints(year_from=2020), 5, asyncio.Event())

    params = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0]).query)
    assert params["search_query"] == [
        'all:"paper agents" AND abs:retrieval '
        "AND submittedDate:[202001010000 TO 999912312359]"
    ]


async def test_arxiv_year_filter_does_not_change_page_offset(tmp_path) -> None:
    transport = FakeTransport({"api/query": ARXIV})
    channel = ArxivChannel(limiter(transport), LocalArtifactStore(tmp_path))
    constraints = SurveyConstraints(year_to=2025)

    await channel.search_page(query("arxiv"), constraints, 1, None, asyncio.Event())
    await channel.search_page(query("arxiv"), constraints, 1, "20", asyncio.Event())

    first = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0]).query)
    second = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[1]).query)
    assert first["start"] == ["0"]
    assert second["start"] == ["20"]
    assert first["search_query"] == [
        'all:"paper agents" AND submittedDate:[100001010000 TO 202512312359]'
    ]


async def test_channels_apply_exact_publication_dates(tmp_path) -> None:
    constraints = SurveyConstraints(
        published_from=date(2024, 2, 3), published_to=date(2025, 3, 13)
    )
    arxiv_transport = FakeTransport({"api/query": ARXIV})
    arxiv = ArxivChannel(limiter(arxiv_transport), LocalArtifactStore(tmp_path / "a"))
    pubmed_transport = FakeTransport(
        {
            "esearch.fcgi": json.dumps({"esearchresult": {"idlist": []}}).encode(),
        }
    )
    pubmed = PubMedChannel(
        limiter(pubmed_transport), LocalArtifactStore(tmp_path / "p")
    )

    await arxiv.search_page(query("arxiv"), constraints, 1, None, asyncio.Event())
    await pubmed.search_page(query("pubmed"), constraints, 1, None, asyncio.Event())

    arxiv_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(arxiv_transport.calls[0]).query
    )
    pubmed_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(pubmed_transport.calls[0]).query
    )
    assert arxiv_query["search_query"] == [
        'all:"paper agents" AND submittedDate:[202402030000 TO 202503132359]'
    ]
    assert pubmed_query["mindate"] == ["2024/02/03"]
    assert pubmed_query["maxdate"] == ["2025/03/13"]


async def test_channels_use_real_page_cursors(tmp_path) -> None:
    arxiv_transport = FakeTransport({"api/query": ARXIV})
    arxiv = ArxivChannel(limiter(arxiv_transport), LocalArtifactStore(tmp_path / "a"))
    arxiv_page = await arxiv.search_page(
        query("arxiv"), SurveyConstraints(), 1, "20", asyncio.Event()
    )
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(arxiv_transport.calls[0]).query)[
        "start"
    ] == ["20"]
    assert arxiv_page.next_cursor == "21"

    openalex_transport = FakeTransport(
        {"api.openalex.org/works?": json.dumps(OPENALEX).encode()}
    )
    openalex = OpenAlexChannel(
        limiter(openalex_transport), LocalArtifactStore(tmp_path / "o")
    )
    openalex_page = await openalex.search_page(
        query("openalex"), SurveyConstraints(), 5, "cursor-1", asyncio.Event()
    )
    assert urllib.parse.parse_qs(
        urllib.parse.urlsplit(openalex_transport.calls[0]).query
    )["cursor"] == ["cursor-1"]
    assert openalex_page.next_cursor == "next-openalex"

    s2_transport = FakeTransport({"paper/search": json.dumps(S2).encode()})
    s2 = SemanticScholarChannel(
        limiter(s2_transport), LocalArtifactStore(tmp_path / "s")
    )
    s2_page = await s2.search_page(
        query("semantic_scholar"), SurveyConstraints(), 5, "10", asyncio.Event()
    )
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(s2_transport.calls[0]).query)[
        "offset"
    ] == ["10"]
    assert s2_page.next_cursor == "5"

    pubmed_transport = FakeTransport(
        {
            "esearch.fcgi": json.dumps(
                {"esearchresult": {"idlist": ["42"], "count": "12"}}
            ).encode(),
            "efetch.fcgi": PUBMED_XML,
        }
    )
    pubmed = PubMedChannel(
        limiter(pubmed_transport), LocalArtifactStore(tmp_path / "p")
    )
    pubmed_page = await pubmed.search_page(
        query("pubmed"), SurveyConstraints(), 5, "10", asyncio.Event()
    )
    assert urllib.parse.parse_qs(
        urllib.parse.urlsplit(pubmed_transport.calls[0]).query
    )["retstart"] == ["10"]
    assert pubmed_page.next_cursor == "11"


async def test_openalex_channel_reconstructs_abstract_and_hints(tmp_path) -> None:
    transport = FakeTransport(
        {"api.openalex.org/works?": json.dumps(OPENALEX).encode()}
    )
    channel = OpenAlexChannel(
        limiter(transport), LocalArtifactStore(tmp_path / "artifacts")
    )

    result = await channel.search(
        query("openalex"), SurveyConstraints(), 5, asyncio.Event()
    )

    assert result[0].identity.openalex_id == "W1"
    assert result[0].abstract == "Paper agents"
    assert result[0].hints[0].kind == "oa_pdf"


async def test_semantic_scholar_channel_maps_external_ids(tmp_path) -> None:
    transport = FakeTransport({"paper/search": json.dumps(S2).encode()})
    channel = SemanticScholarChannel(
        limiter(transport), LocalArtifactStore(tmp_path / "artifacts"), api_key="key"
    )

    result = await channel.search(
        query("semantic_scholar"), SurveyConstraints(), 5, asyncio.Event()
    )

    assert result[0].identity.s2_paper_id == "s2-1"
    assert result[0].identity.arxiv_id == "2501.00001"


async def test_pubmed_channel_joins_esearch_and_efetch(tmp_path) -> None:
    transport = FakeTransport(
        {
            "esearch.fcgi": json.dumps({"esearchresult": {"idlist": ["42"]}}).encode(),
            "efetch.fcgi": PUBMED_XML,
        }
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    channel = PubMedChannel(limiter(transport), artifacts)

    result = await channel.search(
        query("pubmed"), SurveyConstraints(), 5, asyncio.Event()
    )

    assert result[0].identity.pmid == "42"
    assert result[0].identity.pmc_id == "PMC42"
    assert result[0].identity.doi == "10.1000/agents"
    provenance = json.loads(await artifacts.get_text(result[0].raw_response_ref))
    assert set(provenance) == {"query_response_ref", "fetch_response_ref"}


async def test_openalex_semantic_scholar_and_pubmed_reference_endpoints(
    tmp_path,
) -> None:
    openalex_transport = FakeTransport(
        {
            "/works/W1?": json.dumps(
                {"referenced_works": ["https://openalex.org/W2"]}
            ).encode(),
            "filter=openalex_id": json.dumps(OPENALEX).encode(),
        }
    )
    openalex = OpenAlexChannel(
        limiter(openalex_transport), LocalArtifactStore(tmp_path / "openalex")
    )
    openalex_seed = merge_observations(
        [
            CandidateObservation(
                channel="openalex",
                query_id="q",
                query_text="q",
                raw_rank=1,
                identity=ObservedIdentity(openalex_id="W1"),
                title="Seed",
            )
        ]
    )[0][0]
    assert await openalex.references(openalex_seed, 5, asyncio.Event())

    cited = dict(S2["data"][0])
    s2_transport = FakeTransport(
        {"/references?": json.dumps({"data": [{"citedPaper": cited}]}).encode()}
    )
    s2 = SemanticScholarChannel(
        limiter(s2_transport), LocalArtifactStore(tmp_path / "s2")
    )
    s2_seed = merge_observations(
        [
            CandidateObservation(
                channel="arxiv",
                query_id="q",
                query_text="q",
                raw_rank=1,
                identity=ObservedIdentity(arxiv_id="2501.00001"),
                title="Seed",
            )
        ]
    )[0][0]
    assert await s2.references(s2_seed, 5, asyncio.Event())
    assert "ARXIV%3A2501.00001" in s2_transport.calls[0]

    pubmed_transport = FakeTransport(
        {
            "elink.fcgi": json.dumps(
                {"linksets": [{"linksetdbs": [{"links": ["42"]}]}]}
            ).encode(),
            "efetch.fcgi": PUBMED_XML,
        }
    )
    pubmed = PubMedChannel(
        limiter(pubmed_transport), LocalArtifactStore(tmp_path / "pubmed")
    )
    pubmed_seed = merge_observations(
        [
            CandidateObservation(
                channel="pubmed",
                query_id="q",
                query_text="q",
                raw_rank=1,
                identity=ObservedIdentity(pmid="seed"),
                title="Seed",
            )
        ]
    )[0][0]
    assert await pubmed.references(pubmed_seed, 5, asyncio.Event())


class CountingChannel:
    name = "arxiv"
    version = "fixture-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query, constraints, limit, cancel):
        self.calls += 1
        return [
            CandidateObservation(
                channel="arxiv",
                query_id=query.query_id,
                query_text=query.text,
                raw_rank=1,
                identity=ObservedIdentity(arxiv_id="2501.00001"),
                title="Paper Agents",
            )
        ]

    async def references(self, paper, limit, cancel):
        return []


class SearchOnlyChannel(CountingChannel):
    supports_references = False


class PagingChannel(CountingChannel):
    def __init__(self) -> None:
        super().__init__()
        self.cursors: list[str | None] = []

    async def search_page(self, query, constraints, limit, cursor, cancel):
        self.cursors.append(cursor)
        suffix = cursor or "first"
        return SearchPage(
            observations=[
                CandidateObservation(
                    channel="arxiv",
                    query_id=query.query_id,
                    query_text=query.text,
                    raw_rank=1,
                    identity=ObservedIdentity(arxiv_id=f"2501.{len(self.cursors):05d}"),
                    title=f"Paper {suffix}",
                )
            ],
            next_cursor="next" if cursor is None else None,
        )


async def test_channel_cache_is_idempotent_and_exact_replay_miss_is_not_empty(
    tmp_path,
) -> None:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    cache = MemorySurveyCache()
    delegate = CountingChannel()
    cached = CachedChannelAdapter(delegate, artifacts, cache)
    constraints = SurveyConstraints()

    first = await cached.search(query("arxiv"), constraints, 5, asyncio.Event())
    second = await cached.search(query("arxiv"), constraints, 5, asyncio.Event())

    assert first == second
    assert delegate.calls == 1
    replay = ReplayChannelAdapter("arxiv", "fixture-v1", artifacts, cache)
    assert await replay.search(query("arxiv"), constraints, 5, asyncio.Event())
    with pytest.raises(ReplayCacheMiss):
        await replay.search(query("arxiv"), constraints, 6, asyncio.Event())


def test_channel_cache_preserves_reference_capability(tmp_path) -> None:
    cached = CachedChannelAdapter(
        SearchOnlyChannel(),
        LocalArtifactStore(tmp_path / "artifacts"),
        MemorySurveyCache(),
    )

    assert cached.supports_references is False


async def test_page_cache_and_replay_are_cursor_specific(tmp_path) -> None:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    cache = MemorySurveyCache()
    delegate = PagingChannel()
    cached = CachedChannelAdapter(delegate, artifacts, cache)
    constraints = SurveyConstraints()

    first = await cached.search_page(
        query("arxiv"), constraints, 5, None, asyncio.Event()
    )
    second = await cached.search_page(
        query("arxiv"), constraints, 5, "next", asyncio.Event()
    )
    replay = ReplayChannelAdapter("arxiv", "fixture-v1", artifacts, cache)

    assert first != second
    assert delegate.cursors == [None, "next"]
    assert (
        await replay.search_page(query("arxiv"), constraints, 5, None, asyncio.Event())
        == first
    )
    assert (
        await replay.search_page(
            query("arxiv"), constraints, 5, "next", asyncio.Event()
        )
        == second
    )


def test_ucb_rewards_unique_discovery() -> None:
    assert ucb_score("arxiv", {"arxiv": 2}, {"arxiv": 2}, 1.4) > 1.0


def test_search_allocator_covers_query_families_before_reusing_them() -> None:
    budget = budget_for("fast")
    arms = [
        (query_id, channel, f"{query_id}:{channel}")
        for query_id in ("q1", "q2", "q3")
        for channel in ("arxiv", "openalex", "semantic_scholar", "pubmed")
    ]

    selected = allocate_search_indices(arms, {}, {}, {}, {}, budget)

    assert {arms[index][0] for index in selected} == {"q1", "q2", "q3"}
    assert {arms[index][1] for index in selected} == {
        "arxiv",
        "openalex",
        "semantic_scholar",
        "pubmed",
    }

    later = allocate_search_indices(
        arms,
        {"q1": 4, "q2": 4, "q3": 4},
        {arm_key: 1 for _, _, arm_key in arms},
        {"arxiv": 2, "openalex": 2, "semantic_scholar": 2, "pubmed": 2},
        {"arxiv": 4},
        budget,
    )
    assert arms[later[0]][1] == "arxiv"

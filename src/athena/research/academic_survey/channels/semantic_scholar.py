"""Semantic Scholar Graph API 检索与引用通道。"""

import asyncio
import urllib.parse

from athena.research.academic_survey.channels.base import HttpChannel, date_from, text
from athena.research.academic_survey.schemas import (
    CandidateObservation,
    ObservedIdentity,
    SearchPage,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
)
from athena.research.paper_source.schemas import SourceHint

S2_API = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = (
    "paperId,externalIds,title,abstract,authors,year,publicationDate,venue,"
    "citationCount,openAccessPdf"
)


class SemanticScholarChannel(HttpChannel):
    name = "semantic_scholar"
    version = "semantic-scholar-graph-v2"

    def __init__(self, *args, api_key: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.headers = {"x-api-key": api_key} if api_key else None

    async def search(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        return (
            await self.search_page(query, constraints, limit, None, cancel)
        ).observations

    async def search_page(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cursor: str | None,
        cancel: asyncio.Event,
    ) -> SearchPage:
        offset = int(cursor or 0)
        params: dict[str, object] = {
            "query": query.text,
            "limit": min(limit, 100),
            "offset": offset,
            "fields": S2_FIELDS,
        }
        if constraints.year_from is not None or constraints.year_to is not None:
            start = str(constraints.year_from or "")
            end = str(constraints.year_to or "")
            params["year"] = f"{start}-{end}" if start != end else start
        payload, raw_ref = await self.get_json(
            f"{S2_API}/paper/search?{urllib.parse.urlencode(params)}",
            cancel,
            self.headers,
        )
        papers = payload.get("data", []) if isinstance(payload, dict) else []
        next_offset = payload.get("next") if isinstance(payload, dict) else None
        return SearchPage(
            observations=self._observations(papers, query, raw_ref, limit),
            next_cursor=str(next_offset) if next_offset is not None else None,
        )

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        paper_id = (
            paper.identity.s2_paper_id
            or (f"ARXIV:{paper.identity.arxiv_id}" if paper.identity.arxiv_id else None)
            or (f"DOI:{paper.identity.doi}" if paper.identity.doi else None)
            or (f"PMID:{paper.identity.pmid}" if paper.identity.pmid else None)
        )
        if not paper_id:
            return []
        params = {"limit": min(limit, 100), "fields": S2_FIELDS}
        payload, raw_ref = await self.get_json(
            f"{S2_API}/paper/{urllib.parse.quote(paper_id, safe='')}/references?"
            f"{urllib.parse.urlencode(params)}",
            cancel,
            self.headers,
        )
        rows = (payload.get("data") or []) if isinstance(payload, dict) else []
        papers = [
            row.get("citedPaper")
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("citedPaper"), dict)
        ]
        query = SearchQuery(
            query_id=f"ref:{paper.candidate_id}",
            text=paper.title,
            channels=[self.name],
        )
        return self._observations(papers, query, raw_ref, limit)

    def _observations(
        self, papers: object, query: SearchQuery, raw_ref: str, limit: int
    ) -> list[CandidateObservation]:
        if not isinstance(papers, list):
            return []
        result = []
        for rank, paper in enumerate(papers[:limit], start=1):
            if not isinstance(paper, dict) or not (title := text(paper.get("title"))):
                continue
            external = paper.get("externalIds") or {}
            oa = paper.get("openAccessPdf") or {}
            pdf_url = text(oa.get("url")) if isinstance(oa, dict) else ""
            result.append(
                CandidateObservation(
                    channel=self.name,
                    query_id=query.query_id,
                    query_text=query.text,
                    raw_rank=rank,
                    identity=ObservedIdentity(
                        s2_paper_id=text(paper.get("paperId")) or None,
                        arxiv_id=text(external.get("ArXiv")) or None,
                        doi=text(external.get("DOI")) or None,
                        pmid=text(external.get("PubMed")) or None,
                        title=title,
                    ),
                    title=title,
                    abstract=text(paper.get("abstract")),
                    authors=[
                        name
                        for author in paper.get("authors") or []
                        if isinstance(author, dict)
                        and (name := text(author.get("name")))
                    ],
                    year=(
                        paper.get("year")
                        if isinstance(paper.get("year"), int)
                        else None
                    ),
                    published_date=date_from(paper.get("publicationDate")),
                    venue=text(paper.get("venue")),
                    citation_count=(
                        paper.get("citationCount")
                        if isinstance(paper.get("citationCount"), int)
                        else None
                    ),
                    hints=(
                        [
                            SourceHint(
                                url=pdf_url,
                                kind="oa_pdf",
                                channel=self.name,
                                is_open_access=True,
                            )
                        ]
                        if pdf_url
                        else []
                    ),
                    raw_response_ref=raw_ref,
                )
            )
        return result

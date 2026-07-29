"""OpenAlex Works API 检索与单跳引用通道。"""

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
from athena.research.paper_source.openalex import bare_openalex_id, recover_arxiv_id
from athena.research.paper_source.schemas import SourceHint, normalize_doi

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_FIELDS = (
    "id,doi,display_name,publication_year,publication_date,language,cited_by_count,authorships,"
    "primary_location,best_oa_location,open_access,abstract_inverted_index"
)


class OpenAlexChannel(HttpChannel):
    name = "openalex"
    version = "openalex-works-v2"

    def __init__(
        self,
        *args,
        contact_email: str | None = None,
        api_key: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.contact_email = contact_email
        self.api_key = api_key

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
        params = self._params(
            **{
                "search": query.text,
                "per-page": limit,
                "select": OPENALEX_FIELDS,
                "cursor": cursor or "*",
            }
        )
        filters = []
        if constraints.year_from is not None:
            filters.append(f"from_publication_date:{constraints.year_from}-01-01")
        if constraints.year_to is not None:
            filters.append(f"to_publication_date:{constraints.year_to}-12-31")
        if constraints.published_from is not None:
            filters.append(
                f"from_publication_date:{constraints.published_from.isoformat()}"
            )
        if constraints.published_to is not None:
            filters.append(
                f"to_publication_date:{constraints.published_to.isoformat()}"
            )
        if filters:
            params["filter"] = ",".join(filters)
        payload, raw_ref = await self.get_json(
            f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(params)}", cancel
        )
        works = payload.get("results", []) if isinstance(payload, dict) else []
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        next_cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
        return SearchPage(
            observations=self._observations(works, query, raw_ref, limit),
            next_cursor=str(next_cursor) if next_cursor else None,
        )

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        locator = (
            bare_openalex_id(paper.identity.openalex_id)
            if paper.identity.openalex_id
            else f"https://doi.org/{paper.identity.doi}" if paper.identity.doi else None
        )
        if not locator:
            return []
        params = self._params(select="referenced_works")
        record, _ = await self.get_json(
            f"{OPENALEX_WORKS_URL}/{urllib.parse.quote(locator, safe=':/')}?"
            f"{urllib.parse.urlencode(params)}",
            cancel,
        )
        ids = (
            record.get("referenced_works", [])[:limit]
            if isinstance(record, dict)
            else []
        )
        if not ids:
            return []
        bare_ids = [bare_openalex_id(str(value)) for value in ids]
        lookup = self._params(
            filter="openalex_id:" + "|".join(bare_ids),
            **{"per-page": len(bare_ids), "select": OPENALEX_FIELDS},
        )
        payload, raw_ref = await self.get_json(
            f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(lookup)}", cancel
        )
        works = payload.get("results", []) if isinstance(payload, dict) else []
        query = SearchQuery(
            query_id=f"ref:{paper.candidate_id}",
            text=paper.title,
            channels=[self.name],
        )
        return self._observations(works, query, raw_ref, limit)

    def _observations(
        self, works: object, query: SearchQuery, raw_ref: str, limit: int
    ) -> list[CandidateObservation]:
        if not isinstance(works, list):
            return []
        result = []
        for rank, work in enumerate(works[:limit], start=1):
            if not isinstance(work, dict):
                continue
            title = text(work.get("display_name"))
            if not title:
                continue
            best = work.get("best_oa_location") or {}
            source = (work.get("primary_location") or {}).get("source") or {}
            pdf_url = text(best.get("pdf_url")) if isinstance(best, dict) else ""
            hints = []
            if pdf_url:
                hints.append(
                    SourceHint(
                        url=pdf_url,
                        kind="oa_pdf",
                        channel=self.name,
                        is_open_access=True,
                        license=text(best.get("license")) or None,
                    )
                )
            result.append(
                CandidateObservation(
                    channel=self.name,
                    query_id=query.query_id,
                    query_text=query.text,
                    raw_rank=rank,
                    identity=ObservedIdentity(
                        openalex_id=bare_openalex_id(text(work.get("id"))),
                        doi=normalize_doi(text(work.get("doi"))) or None,
                        arxiv_id=recover_arxiv_id(work) or None,
                        title=title,
                    ),
                    title=title,
                    abstract=_abstract(work.get("abstract_inverted_index")),
                    authors=_authors(work.get("authorships")),
                    year=(
                        work.get("publication_year")
                        if isinstance(work.get("publication_year"), int)
                        else None
                    ),
                    published_date=date_from(work.get("publication_date")),
                    venue=(
                        text(source.get("display_name"))
                        if isinstance(source, dict)
                        else ""
                    ),
                    language=text(work.get("language")),
                    citation_count=(
                        work.get("cited_by_count")
                        if isinstance(work.get("cited_by_count"), int)
                        else None
                    ),
                    hints=hints,
                    raw_response_ref=raw_ref,
                )
            )
        return result

    def _params(self, **values: object) -> dict[str, object]:
        if self.contact_email:
            values["mailto"] = self.contact_email
        if self.api_key:
            values["api_key"] = self.api_key
        return values


def _abstract(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    positions = [
        (position, str(word))
        for word, offsets in value.items()
        if isinstance(offsets, list)
        for position in offsets
        if isinstance(position, int)
    ]
    return " ".join(word for _, word in sorted(positions))


def _authors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        name
        for item in value
        if isinstance(item, dict)
        and (name := text((item.get("author") or {}).get("display_name")))
    ]

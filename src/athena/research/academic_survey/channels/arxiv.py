"""arXiv Atom API 检索通道。"""

import asyncio
import re
import urllib.parse

from athena.research.academic_survey.channels.base import (
    HttpChannel,
    date_from,
    text,
    year_from,
)
from athena.research.academic_survey.schemas import (
    CandidateObservation,
    ObservedIdentity,
    SearchPage,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
)
from athena.research.paper_source.arxiv import ARXIV_QUERY_URL, parse_atom_feed
from athena.research.paper_source.schemas import SourceHint

_FIELD_PREFIX = re.compile(r"(?:^|[\s(])(?:all|ti|au|abs|co|jr|cat|rn|id):", re.I)


class ArxivChannel(HttpChannel):
    name = "arxiv"
    version = "arxiv-atom-v2"
    supports_references = False

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
        expression = query.text.strip()
        if not _FIELD_PREFIX.search(expression):
            expression = f'all:"{expression}"'
        if any(
            (
                constraints.year_from,
                constraints.year_to,
                constraints.published_from,
                constraints.published_to,
            )
        ):
            date_min = constraints.published_from
            date_max = constraints.published_to
            start = (
                date_min.strftime("%Y%m%d")
                if date_min
                else f"{constraints.year_from or 1000}0101"
            )
            end = (
                date_max.strftime("%Y%m%d")
                if date_max
                else f"{constraints.year_to or 9999}1231"
            )
            expression += f" AND submittedDate:[{start}0000 TO {end}2359]"
        params = urllib.parse.urlencode(
            {
                "search_query": expression,
                "start": offset,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        response, raw_ref = await self.get(f"{ARXIV_QUERY_URL}?{params}", cancel)
        metadata = list(parse_atom_feed(response.body).values())
        observations = [
            CandidateObservation(
                channel=self.name,
                query_id=query.query_id,
                query_text=query.text,
                raw_rank=rank,
                identity=ObservedIdentity(
                    arxiv_id=item.arxiv_id,
                    doi=item.doi,
                    title=item.title,
                ),
                title=item.title,
                abstract=item.abstract,
                authors=item.authors,
                year=year_from(item.published),
                published_date=date_from(item.published),
                venue=text(item.journal_ref),
                hints=[
                    SourceHint(
                        url=f"https://arxiv.org/abs/{item.arxiv_id}",
                        kind="arxiv_abs",
                        channel=self.name,
                        is_open_access=True,
                    )
                ],
                raw_response_ref=raw_ref,
            )
            for rank, item in enumerate(metadata[:limit], start=1)
            if item.title
        ]
        return SearchPage(
            observations=observations,
            next_cursor=(
                str(offset + len(observations)) if len(observations) == limit else None
            ),
        )

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        """Atom API 不提供结构化引用。"""
        return []

"""arXiv Atom API 检索通道。"""

import asyncio
import re
import urllib.parse

from athena.research.academic_survey.channels.base import HttpChannel, text, year_from
from athena.research.academic_survey.schemas import (
    CandidateObservation,
    ObservedIdentity,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
)
from athena.research.paper_source.arxiv import ARXIV_QUERY_URL, parse_atom_feed
from athena.research.paper_source.schemas import SourceHint

_FIELD_PREFIX = re.compile(r"(?:^|[\s(])(?:all|ti|au|abs|co|jr|cat|rn|id):", re.I)


class ArxivChannel(HttpChannel):
    name = "arxiv"
    version = "arxiv-atom-v1"

    async def search(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        expression = query.text.strip()
        if not _FIELD_PREFIX.search(expression):
            expression = f'all:"{expression}"'
        if constraints.year_from is not None or constraints.year_to is not None:
            start = constraints.year_from or 1000
            end = constraints.year_to or 9999
            expression += f" AND submittedDate:[{start}01010000 TO {end}12312359]"
        params = urllib.parse.urlencode(
            {
                "search_query": expression,
                "start": 0,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        response, raw_ref = await self.get(f"{ARXIV_QUERY_URL}?{params}", cancel)
        metadata = list(parse_atom_feed(response.body).values())
        return [
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

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        """Atom API 不提供结构化引用。"""
        return []

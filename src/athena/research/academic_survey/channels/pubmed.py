"""NCBI PubMed E-utilities 检索与引用通道。"""

import asyncio
import json
import urllib.parse
from xml.etree import ElementTree

from athena.research.academic_survey.channels.base import (
    HttpChannel,
    RetryableChannelFailure,
    text,
    year_from,
)
from athena.research.academic_survey.schemas import (
    CandidateObservation,
    ObservedIdentity,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
)
from athena.research.paper_source.schemas import SourceHint

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class PubMedChannel(HttpChannel):
    name = "pubmed"
    version = "pubmed-eutils-v1"

    def __init__(
        self,
        *args,
        api_key: str | None = None,
        contact_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.api_key = api_key
        self.contact_email = contact_email

    async def search(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        params = self._params(
            db="pubmed",
            retmode="json",
            term=query.text,
            retmax=limit,
            sort="relevance",
        )
        if constraints.year_from is not None:
            params["mindate"] = str(constraints.year_from)
            params["datetype"] = "pdat"
        if constraints.year_to is not None:
            params["maxdate"] = str(constraints.year_to)
            params["datetype"] = "pdat"
        payload, search_ref = await self.get_json(
            f"{EUTILS}/esearch.fcgi?{urllib.parse.urlencode(params)}", cancel
        )
        ids = (
            (payload.get("esearchresult") or {}).get("idlist", [])
            if isinstance(payload, dict)
            else []
        )
        return await self._fetch(ids[:limit], query, search_ref, cancel)

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        if not paper.identity.pmid:
            return []
        params = self._params(
            dbfrom="pubmed",
            db="pubmed",
            id=paper.identity.pmid,
            linkname="pubmed_pubmed_refs",
            retmode="json",
        )
        payload, link_ref = await self.get_json(
            f"{EUTILS}/elink.fcgi?{urllib.parse.urlencode(params)}", cancel
        )
        ids = []
        if isinstance(payload, dict):
            for linkset in payload.get("linksets", []):
                if not isinstance(linkset, dict):
                    continue
                for database in linkset.get("linksetdbs", []):
                    if isinstance(database, dict):
                        ids.extend(str(value) for value in database.get("links", []))
        query = SearchQuery(
            query_id=f"ref:{paper.candidate_id}",
            text=paper.title,
            channels=[self.name],
        )
        return await self._fetch(ids[:limit], query, link_ref, cancel)

    async def _fetch(
        self,
        ids: list[str],
        query: SearchQuery,
        source_ref: str,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        if not ids:
            return []
        params = self._params(db="pubmed", id=",".join(ids), retmode="xml")
        response, fetch_ref = await self.get(
            f"{EUTILS}/efetch.fcgi?{urllib.parse.urlencode(params)}", cancel
        )
        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError as error:
            raise RetryableChannelFailure("PubMed returned invalid XML") from error
        provenance_ref = await self.artifacts.put_text(
            json.dumps(
                {"query_response_ref": source_ref, "fetch_response_ref": fetch_ref}
            )
        )
        articles = {_pmid(node): node for node in root.findall(".//PubmedArticle")}
        return [
            item
            for rank, pmid in enumerate(ids, start=1)
            if (node := articles.get(str(pmid))) is not None
            and (
                item := _observation(
                    node,
                    query,
                    rank,
                    provenance_ref,
                )
            )
            is not None
        ]

    def _params(self, **values: object) -> dict[str, object]:
        params = {"tool": "athena_academic_survey", **values}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.contact_email:
            params["email"] = self.contact_email
        return params


def _observation(
    node: ElementTree.Element,
    query: SearchQuery,
    rank: int,
    raw_ref: str,
) -> CandidateObservation | None:
    pmid = _pmid(node)
    article = node.find(".//Article")
    if article is None:
        return None
    title = _node_text(article.find("ArticleTitle"))
    if not title:
        return None
    ids = {
        text(item.get("IdType")).casefold(): text(item.text)
        for item in node.findall("PubmedData/ArticleIdList/ArticleId")
    }
    year = year_from(
        article.findtext("Journal/JournalIssue/PubDate/Year")
        or article.findtext("Journal/JournalIssue/PubDate/MedlineDate")
    )
    pmc_id = ids.get("pmc") or None
    hints = []
    if pmc_id:
        hints.append(
            SourceHint(
                url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/pdf/",
                kind="oa_pdf",
                channel="pubmed",
                is_open_access=True,
            )
        )
    return CandidateObservation(
        channel="pubmed",
        query_id=query.query_id,
        query_text=query.text,
        raw_rank=rank,
        identity=ObservedIdentity(
            pmid=pmid,
            pmc_id=pmc_id,
            doi=ids.get("doi") or None,
            title=title,
        ),
        title=title,
        abstract=" ".join(
            value
            for item in article.findall("Abstract/AbstractText")
            if (value := _node_text(item))
        ),
        authors=[
            name
            for author in article.findall("AuthorList/Author")
            if (
                name := text(
                    " ".join(
                        filter(
                            None,
                            [
                                author.findtext("ForeName"),
                                author.findtext("LastName"),
                            ],
                        )
                    )
                )
            )
        ],
        year=year,
        venue=text(article.findtext("Journal/Title")),
        language=text(article.findtext("Language")),
        hints=hints,
        raw_response_ref=raw_ref,
    )


def _pmid(node: ElementTree.Element) -> str:
    return text(node.findtext(".//MedlineCitation/PMID"))


def _node_text(node: ElementTree.Element | None) -> str:
    return text("".join(node.itertext())) if node is not None else ""

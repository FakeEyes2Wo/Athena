"""Retriever: paper + HF model search with degrade chain.

Aligns with Codex web_search.rs — a search provider with fallback chain.
"""

import asyncio
import hashlib
import inspect
import json
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Sequence

from athena.retrieval.types import HFModelRef, PaperRef


class RecoverableRetrievalError(RuntimeError):
    """An integration failure that permits the next retrieval source."""


_RECOVERABLE_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    ET.ParseError,
)


async def _call_maybe_sync(func, *args, **kwargs):
    """Call an integration method without blocking the event loop."""
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    result = await asyncio.to_thread(func, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def retrieve_papers(query: str, *, adapters: Sequence[object]) -> list[PaperRef]:
    """Query adapters in declaration order, degrading only on known failures."""
    for adapter in adapters:
        search = getattr(adapter, "search", adapter)
        try:
            papers = await _call_maybe_sync(search, query)
        except RecoverableRetrievalError:
            continue
        if papers:
            if not isinstance(papers, list) or not all(
                isinstance(paper, PaperRef) for paper in papers
            ):
                raise TypeError("retrieval adapters must return list[PaperRef]")
            return papers
    return [common_knowledge_ref(query)]


def common_knowledge_ref(query: str) -> PaperRef:
    """Create an explicitly labelled no-source fallback reference."""
    return PaperRef(
        title=f"Common knowledge for {query}",
        source="common_knowledge",
        key_findings="No retrieved source was available; this is common-knowledge context.",
    )


def sha256_path(path: Path) -> str:
    """Hash a downloaded file or directory deterministically."""
    digest = hashlib.sha256()
    files = (
        [path]
        if path.is_file()
        else sorted(item for item in path.rglob("*") if item.is_file())
    )
    if not files:
        raise ValueError("downloaded artifact must contain at least one file")
    for file_path in files:
        digest.update(str(file_path.relative_to(path.parent)).encode("utf-8"))
        with file_path.open("rb") as payload:
            for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


async def download_model(
    repo: str,
    revision: str,
    *,
    client,
    put_path: Callable[[Path], str],
) -> HFModelRef:
    """Download a revision-pinned model and retain immutable provenance."""
    if not revision.strip():
        raise ValueError("revision must be pinned")
    metadata = await _call_maybe_sync(client.metadata, repo, revision)
    path = Path(
        await _call_maybe_sync(
            client.download,
            repo,
            revision,
            trust_remote_code=False,
        )
    )
    license_name = (
        metadata.get("license", "") if isinstance(metadata, dict) else metadata.license
    )
    return HFModelRef(
        repo=repo,
        revision=revision,
        license=license_name,
        digest=sha256_path(path),
        artifact=await _call_maybe_sync(put_path, path),
    )


class Retriever:
    """Paper + HuggingFace model search with degrade chain. Never blocks pipeline."""

    async def search_papers(self, query: str, n: int = 5) -> list[PaperRef]:
        """Search papers: arxiv → semantic_scholar → web → empty."""

        async def arxiv(search_query: str) -> list[PaperRef]:
            return await self._search_arxiv(search_query, n)

        async def semantic_scholar(search_query: str) -> list[PaperRef]:
            return await self._search_semantic_scholar(search_query, n)

        async def web(search_query: str) -> list[PaperRef]:
            return await self._search_web(search_query, n)

        return await retrieve_papers(query, adapters=(arxiv, semantic_scholar, web))

    async def search_models(self, query: str, n: int = 3) -> list[HFModelRef]:
        """Search HuggingFace Hub for pretrained models."""
        return await self._search_huggingface(query, n)

    async def _search_arxiv(self, query: str, n: int) -> list[PaperRef]:
        """Search arxiv API for papers matching the query."""
        try:
            return await asyncio.to_thread(self._search_arxiv_sync, query, n)
        except _RECOVERABLE_NETWORK_ERRORS as error:
            raise RecoverableRetrievalError("arxiv retrieval failed") from error

    @staticmethod
    def _search_arxiv_sync(query: str, n: int) -> list[PaperRef]:
        encoded = urllib.parse.quote(query)
        url = f"http://export.arxiv.org/api/query?search_query=all:{encoded}&max_results={n}"
        req = urllib.request.Request(url, headers={"User-Agent": "Athena/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        papers = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            link_el = entry.find("atom:id", ns)
            papers.append(
                PaperRef(
                    title=(title_el.text or "").strip().replace("\n", " "),
                    source="arxiv",
                    url=(link_el.text or "").strip() if link_el is not None else "",
                    key_findings=(
                        (summary_el.text or "")[:500].strip()
                        if summary_el is not None
                        else ""
                    ),
                )
            )
        return papers[:n]

    async def _search_semantic_scholar(self, query: str, n: int) -> list[PaperRef]:
        """Search Semantic Scholar API for papers matching the query."""
        try:
            return await asyncio.to_thread(self._search_semantic_scholar_sync, query, n)
        except _RECOVERABLE_NETWORK_ERRORS as error:
            raise RecoverableRetrievalError(
                "Semantic Scholar retrieval failed"
            ) from error

    @staticmethod
    def _search_semantic_scholar_sync(query: str, n: int) -> list[PaperRef]:
        encoded = urllib.parse.quote(query)
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search?"
            f"query={encoded}&limit={n}&fields=title,url,abstract"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Athena/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [
            PaperRef(
                title=paper.get("title", ""),
                source="semantic_scholar",
                url=paper.get("url", ""),
                key_findings=paper.get("abstract", "")[:500],
            )
            for paper in data.get("data", [])
        ][:n]

    async def _search_web(self, query: str, n: int) -> list[PaperRef]:
        """Web search degrade. Returns empty — needs external API key."""
        return []

    async def _search_huggingface(self, query: str, n: int) -> list[HFModelRef]:
        """Search HuggingFace Hub API for pretrained models."""
        try:
            return await asyncio.to_thread(self._search_huggingface_sync, query, n)
        except _RECOVERABLE_NETWORK_ERRORS:
            return []

    @staticmethod
    def _search_huggingface_sync(query: str, n: int) -> list[HFModelRef]:
        encoded = urllib.parse.quote(query)
        url = (
            f"https://huggingface.co/api/models?"
            f"search={encoded}&limit={n}&sort=downloads&direction=-1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Athena/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [
            HFModelRef(
                repo=model.get("modelId", model.get("id", "")),
                license=model.get("license", ""),
                task_match=model.get("pipeline_tag", ""),
            )
            for model in data
        ][:n]


if __name__ == "__main__":

    async def _demo():
        retriever = Retriever()
        papers = await retriever.search_papers("transformer attention", n=2)
        print(f"Papers found: {len(papers)}")

    asyncio.run(_demo())

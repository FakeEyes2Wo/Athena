"""Graph and document traversal for an indexed paper corpus.

Retrieval ranking lives in :mod:`search`; this module only follows typed
links, sections, and read state after a hit has been selected.
"""

from typing import TYPE_CHECKING

from athena.research.literature.paper_rag.schemas import ChunkRead, SearchHit

if TYPE_CHECKING:
    from athena.research.literature.paper_rag.search import (
        LoadedCorpus,
        RetrievalSession,
    )

MAX_SNIPPET_CHARS = 600
TRAVERSAL_SNIPPET_SENTENCES = 3
ALREADY_READ_NOTICE = "This chunk has been read before."
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "limitation": ("limitation", "threats to validity", "shortcoming", "failure case"),
    "ablation": (
        "ablation",
        "component analysis",
        "sensitivity analysis",
        "factor analysis",
    ),
    "related work": ("related work", "background", "prior work", "literature"),
    "experiment": ("experiment", "empirical", "evaluation", "results"),
    "conclusion": ("conclusion", "concluding", "summary and outlook"),
    "discussion": ("discussion", "analysis and discussion"),
    "method": ("method", "methodology", "approach", "our model", "proposed"),
    "dataset": ("dataset", "data set", "data collection", "corpus", "benchmark"),
}


def heading_variants(heading: str) -> tuple[str, ...]:
    """Expand a section label into the aliases used by paper headings."""
    wanted = heading.lower().strip()
    for aliases in SECTION_ALIASES.values():
        if any(alias in wanted for alias in aliases):
            return (wanted, *aliases)
    return (wanted,)


def join_snippet(sentences: list[str]) -> str:
    """Join matching sentences into a bounded display snippet."""
    text = " ".join(part.strip() for part in sentences if part.strip())
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    return text[:MAX_SNIPPET_CHARS].rstrip() + " …"


def make_hit(
    corpus: "LoadedCorpus", position: int, score: float, snippet: str
) -> SearchHit:
    """Build one search hit from an indexed chunk position."""
    entry = corpus.index.entries[position]
    return SearchHit(
        chunk_id=entry.chunk_id,
        paper_id=entry.paper_id,
        title=entry.title,
        kind=entry.kind,
        heading_path=entry.heading_path,
        score=score,
        snippet=snippet,
        visual_ids=entry.visual_ids,
        cited_ids=entry.cited_ids,
    )


def _head_snippet(corpus: "LoadedCorpus", position: int) -> str:
    entry = corpus.index.entries[position]
    return join_snippet(
        [
            corpus.index.sentence_text(i)
            for i in range(entry.sentence_start, entry.sentence_end)
        ][:TRAVERSAL_SNIPPET_SENTENCES]
    )


def traverse(corpus: "LoadedCorpus", targets: list[str]) -> list[SearchHit]:
    """Return linked chunks in first-seen order, with short orientation snippets."""
    results: list[SearchHit] = []
    seen: set[str] = set()
    for chunk_id in targets:
        position = corpus.positions.get(chunk_id)
        if position is None or chunk_id in seen:
            continue
        seen.add(chunk_id)
        results.append(make_hit(corpus, position, 1.0, _head_snippet(corpus, position)))
    return results


def edge_targets(corpus: "LoadedCorpus", chunk_ids: list[str], edge: str) -> list[str]:
    """Resolve an entry edge (``visual_ids`` or ``cited_ids``) in input order."""
    targets: list[str] = []
    for chunk_id in chunk_ids:
        position = corpus.positions.get(chunk_id)
        if position is None:
            continue
        targets.extend(getattr(corpus.index.entries[position], edge))
    return targets


def paper_namespace(chunk_id: str) -> str:
    """Extract the paper namespace from a paper-namespaced chunk id."""
    return chunk_id.rsplit(":", 1)[0]


def citing_anchors(corpus: "LoadedCorpus", chunk_ids: list[str]) -> list[str]:
    """Return cited-paper anchors for the supplied chunks."""
    wanted = {paper_namespace(chunk_id) for chunk_id in chunk_ids}
    return [anchor for anchor in corpus.cited_by if paper_namespace(anchor) in wanted]


def visual_links(corpus: "LoadedCorpus", chunk_ids: list[str]) -> list[SearchHit]:
    """Follow visual links from the supplied chunks."""
    return traverse(corpus, edge_targets(corpus, chunk_ids, "visual_ids"))


def citation_links(
    corpus: "LoadedCorpus", chunk_ids: list[str], direction: str
) -> list[SearchHit]:
    """Follow either outgoing citations or incoming citing-paper links."""
    if direction != "cited_by":
        return traverse(corpus, edge_targets(corpus, chunk_ids, "cited_ids"))
    anchors = sorted(citing_anchors(corpus, chunk_ids))
    return traverse(
        corpus, [target for anchor in anchors for target in corpus.cited_by[anchor]]
    )


def section_search(
    corpus: "LoadedCorpus", heading: str, paper_ids: list[str], limit: int
) -> list[SearchHit]:
    """Find matching sections and interleave results by paper."""
    variants = heading_variants(heading)
    allowed = {item for item in paper_ids if item}
    by_paper: dict[str, list[tuple[float, int]]] = {}
    for position, entry in enumerate(corpus.index.entries):
        if allowed and entry.paper_id not in allowed:
            continue
        depth = next(
            (
                level
                for level, name in enumerate(entry.heading_path)
                if any(variant in name.lower() for variant in variants)
            ),
            None,
        )
        if depth is not None:
            by_paper.setdefault(entry.paper_id, []).append(
                (1.0 / (1 + depth), position)
            )
    for group in by_paper.values():
        group.sort(key=lambda item: (-item[0], item[1]))
    order = sorted(by_paper, key=lambda paper: (-by_paper[paper][0][0], paper))
    rounds = max((len(group) for group in by_paper.values()), default=0)
    picked = [
        by_paper[paper][index]
        for index in range(rounds)
        for paper in order
        if index < len(by_paper[paper])
    ]
    return [
        make_hit(corpus, position, score, _head_snippet(corpus, position))
        for score, position in picked[:limit]
    ]


def read_one(
    corpus: "LoadedCorpus", session: "RetrievalSession", position: int
) -> ChunkRead:
    """Read one chunk, returning a notice when this session saw it already."""
    entry = corpus.index.entries[position]
    if session.was_read(entry.chunk_id):
        return ChunkRead(
            chunk_id=entry.chunk_id,
            status="already_read",
            title=entry.title,
            heading_path=entry.heading_path,
            text=ALREADY_READ_NOTICE,
        )
    session.mark_read(entry.chunk_id)
    return ChunkRead(
        chunk_id=entry.chunk_id,
        status="read",
        title=entry.title,
        heading_path=entry.heading_path,
        text=entry.text,
        visual_ids=entry.visual_ids,
        cited_ids=entry.cited_ids,
    )


def read_chunks(
    corpus: "LoadedCorpus",
    session: "RetrievalSession",
    chunk_ids: list[str],
    include_adjacent: bool,
) -> list[ChunkRead]:
    """Read requested chunks and optional adjacent chunks in input order."""
    results: list[ChunkRead] = []
    seen: set[int] = set()
    for chunk_id in chunk_ids:
        position = corpus.positions.get(chunk_id)
        if position is None:
            results.append(ChunkRead(chunk_id=chunk_id, status="not_found"))
            continue
        targets = [position]
        if include_adjacent:
            paper_id = corpus.index.entries[position].paper_id
            targets = [
                other
                for other in (position - 1, position, position + 1)
                if 0 <= other < len(corpus.index.entries)
                and corpus.index.entries[other].paper_id == paper_id
            ]
        for target in targets:
            if target in seen:
                continue
            seen.add(target)
            results.append(read_one(corpus, session, target))
    return results


__all__ = [
    "citation_links",
    "citing_anchors",
    "edge_targets",
    "heading_variants",
    "join_snippet",
    "make_hit",
    "paper_namespace",
    "read_chunks",
    "read_one",
    "section_search",
    "traverse",
    "visual_links",
]

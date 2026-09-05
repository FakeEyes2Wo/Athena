"""Corpus-facing helpers for ``ResearchRuntime``.

Keeps paper-tool plumbing out of the composition root. Each function takes the
runtime as an explicit dependency, so the runtime can remain a thin facade over
the collaborator modules it owns.
"""

from typing import Any

from athena.core.tool import ToolRegistry
from athena.research.literature.paper_rag.models import PaperSummary
from athena.research.literature.paper_rag.search import (
    RetrievalSession,
    corpus_overview,
)
from athena.research.literature.paper_rag.search import (
    corpus_paper_ids as paper_corpus_paper_ids,
)
from athena.research.literature.paper_rag.tool import MAX_OVERVIEW_PAPERS
from athena.research.literature.survey import build_survey_tools


def corpus_tools(runtime: Any, *, for_ideation: bool = False) -> ToolRegistry | None:
    """Return read-only paper operators for the current corpus, if any."""
    if runtime.survey_corpus_ref() is None:
        return None
    session = RetrievalSession(runtime.ensure_survey_stack().corpus_cache)
    if for_ideation:
        # Each Ideator gets its own read ledger, but the runtime owns the list
        # so citation verification can ask "who actually opened what this round".
        runtime.session.survey.corpus_sessions.append(session)
    return build_survey_tools(
        runtime.ensure_survey_stack(),
        include_survey=False,
        include_producers=False,
        session=session,
    )


def start_corpus_round(runtime: Any) -> None:
    """Drop the previous ideation round's read ledger."""
    runtime.session.survey.corpus_sessions.clear()


def corpus_papers_read(runtime: Any) -> set[str]:
    """Paper ids actually opened during this ideation round."""
    opened: set[str] = set()
    for session in runtime.session.survey.corpus_sessions:
        opened |= session.read_papers()
    return opened


async def corpus_passages_read(runtime: Any) -> dict[str, list[str]]:
    """Passages actually read this round, grouped by paper id."""
    corpus_ref = runtime.survey_corpus_ref()
    if corpus_ref is None:
        return {}
    chunk_ids: set[str] = set()
    for session in runtime.session.survey.corpus_sessions:
        chunk_ids |= session.read_chunk_ids()
    if not chunk_ids:
        return {}
    stack = runtime.ensure_survey_stack()
    corpus = await stack.corpus_cache.load(runtime.store, corpus_ref)
    passages: dict[str, list[str]] = {}
    for chunk_id in sorted(chunk_ids):
        position = corpus.positions.get(chunk_id)
        if position is None:
            continue
        entry = corpus.index.entries[position]
        passages.setdefault(entry.paper_id, []).append(entry.text)
    return passages


async def corpus_summaries(runtime: Any) -> list[PaperSummary]:
    """One-line summaries for every paper in the active corpus."""
    corpus_ref = runtime.survey_corpus_ref()
    if corpus_ref is None:
        return []
    stack = runtime.ensure_survey_stack()
    corpus = await stack.corpus_cache.load(runtime.store, corpus_ref)
    return corpus_overview(corpus, [], MAX_OVERVIEW_PAPERS).summaries


async def corpus_paper_ids(runtime: Any) -> set[str]:
    """Paper ids that actually exist in the active corpus."""
    corpus_ref = runtime.survey_corpus_ref()
    if corpus_ref is None:
        return set()
    stack = runtime.ensure_survey_stack()
    corpus = await stack.corpus_cache.load(runtime.store, corpus_ref)
    return paper_corpus_paper_ids(corpus)

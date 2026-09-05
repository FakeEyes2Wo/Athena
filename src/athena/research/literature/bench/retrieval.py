"""Known-item retrieval benchmark and its versioned dataset I/O."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from athena.research.literature.bench.models import (
    ChannelScore,
    KnownItemQuery,
    QueryOutcome,
    QuerySet,
    RecallQuerySet,
    RetrievalBenchReport,
)
from athena.research.literature.paper_rag.index import normalize
from athena.research.literature.paper_rag.interfaces import TextEmbedder
from athena.research.literature.paper_rag.schemas import SearchHit
from athena.research.literature.paper_rag.search import (
    LoadedCorpus,
    corpus_paper_ids,
    hybrid_search,
    keyword_search,
    semantic_search,
)

DEFAULT_QUERY_SET = "imbalance_auc"
DEFAULT_TOP_K = 10
KEYWORD_CHANNEL = "paper_keyword_search"
SEMANTIC_CHANNEL = "paper_semantic_search"
HYBRID_CHANNEL = "hybrid_rrf"

_DATASETS = Path(__file__).parent / "datasets"
_Model = TypeVar("_Model", bound=BaseModel)
_Runner = Callable[[KnownItemQuery, int], Awaitable[list[SearchHit]]]


def available() -> list[str]:
    """Return packaged benchmark dataset names."""
    return sorted(path.stem for path in _DATASETS.glob("*.json"))


def _load(model: type[_Model], name_or_path: str, kind: str) -> _Model:
    candidate = Path(name_or_path)
    path = candidate if candidate.suffix == ".json" or candidate.exists() else None
    if path is None:
        path = _DATASETS / f"{name_or_path}.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"unknown {kind} {name_or_path!r}; packaged sets: "
                f"{', '.join(available())}"
            )
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def load_query_set(name_or_path: str = DEFAULT_QUERY_SET) -> QuerySet:
    return _load(QuerySet, name_or_path, "query set")


def load_recall_set(name_or_path: str) -> RecallQuerySet:
    return _load(RecallQuerySet, name_or_path, "recall set")


def dump_report(report: BaseModel, path: str | Path) -> Path:
    """Write a benchmark report as diff-friendly JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _paper_rank(hits: list[SearchHit], gold: list[str]) -> tuple[int | None, list[str]]:
    papers = list(dict.fromkeys(hit.paper_id for hit in hits if hit.paper_id))
    wanted = set(gold)
    rank = next(
        (index for index, paper in enumerate(papers, 1) if paper in wanted), None
    )
    return rank, papers


async def run_known_item(
    corpus: LoadedCorpus,
    query_set: QuerySet,
    *,
    corpus_ref: str = "",
    embedder: TextEmbedder | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> RetrievalBenchReport:
    """Run each available retrieval channel over a known-item query set."""
    present = corpus_paper_ids(corpus)
    usable = [
        query
        for query in query_set.queries
        if any(gold in present for gold in query.gold)
    ]
    unusable = [
        query.query_id
        for query in query_set.queries
        if not any(gold in present for gold in query.gold)
    ]

    async def keyword(query: KnownItemQuery, limit: int) -> list[SearchHit]:
        return keyword_search(corpus, query.keywords or query.question.split(), limit)

    async def run(channel: str, runner: _Runner) -> ChannelScore:
        outcomes: list[QueryOutcome] = []
        for query in usable:
            try:
                hits = await runner(query, top_k)
            except Exception as error:  # noqa: BLE001 - isolate one benchmark query
                outcomes.append(
                    QueryOutcome(
                        query_id=query.query_id,
                        error=f"{type(error).__name__}: {error}",
                    )
                )
                continue
            rank, papers = _paper_rank(hits, query.gold)
            outcomes.append(
                QueryOutcome(
                    query_id=query.query_id,
                    rank=rank,
                    returned_papers=papers,
                )
            )
        return ChannelScore(channel=channel, outcomes=outcomes)

    channels = [await run(KEYWORD_CHANNEL, keyword)]
    if embedder is not None and corpus.has_vectors():

        async def semantic(query: KnownItemQuery, limit: int) -> list[SearchHit]:
            vectors = await embedder.embed([query.question])
            return (
                semantic_search(corpus, normalize(vectors[0]), limit) if vectors else []
            )

        async def hybrid(query: KnownItemQuery, limit: int) -> list[SearchHit]:
            vectors = await embedder.embed([query.question])
            vector = normalize(vectors[0]) if vectors else []
            return hybrid_search(
                corpus, vector, query.keywords or query.question.split(), limit
            )

        channels.extend(
            [
                await run(SEMANTIC_CHANNEL, semantic),
                await run(HYBRID_CHANNEL, hybrid),
            ]
        )

    return RetrievalBenchReport(
        query_set=query_set.name,
        corpus_ref=corpus_ref,
        corpus_papers=len(present),
        corpus_chunks=len(corpus.index.entries),
        embedding_model=corpus.index.embedding_model,
        unusable_queries=unusable,
        channels=channels,
        top_k=top_k,
    )

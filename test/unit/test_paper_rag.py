"""Tests for the A-RAG hierarchical retrieval interfaces."""

import asyncio
import tempfile
import unittest

from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext
from athena.research.paper_markdown.schemas import (
    PaperChunk,
    PaperContent,
    PaperProvenance,
)
from athena.research.paper_rag.contextual import contextualize_entries
from athena.research.paper_rag.index import (
    build_corpus_index,
    is_indexable,
    split_sentences,
)
from athena.research.paper_rag.schemas import PaperCorpusIndex
from athena.research.paper_rag.search import (
    ALREADY_READ_NOTICE,
    RetrievalSession,
    keyword_search,
    read_chunks,
    self_contained_weight,
    semantic_search,
)
from athena.research.paper_rag.tool import (
    PaperChunkReadTool,
    PaperKeywordSearchTool,
    PaperSemanticSearchTool,
)
from athena.storage import LocalArtifactStore

VOCABULARY = ["retrieval", "grasping"]


class FakeEmbedder:
    """Bag-of-word-counts embedder — deterministic and dependency free."""

    def __init__(self, vocabulary: list[str], model: str = "fake-embed-1") -> None:
        self.vocabulary = vocabulary
        self.model = model
        self.batches: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [
            [float(text.lower().count(word)) for word in self.vocabulary]
            for text in texts
        ]


async def noop_emit(kind: str, ref: str, data: dict | None = None) -> None:
    pass


def context(name: str) -> ToolContext:
    """Build a tool context for direct tool invocation."""
    return ToolContext(name, "test-call", noop_emit, asyncio.Event())


async def make_paper(
    store: LocalArtifactStore, paper_id: str, title: str, texts: list[str]
) -> PaperContent:
    """Persist a minimal PaperContent whose chunks carry the given retrieval texts."""
    chunks = []
    for position, text in enumerate(texts):
        chunks.append(
            PaperChunk(
                chunk_id=f"c{position}",
                kind="paragraph",
                content_ref=await store.put_text(text),
                heading_path=["Method"],
                char_start=0,
                char_end=len(text),
                token_estimate=len(text) // 4,
            )
        )
    blank = await store.put_text(f"# {title}")
    return PaperContent(
        paper_id=paper_id,
        title=title,
        provenance=PaperProvenance(
            source_kind="tex",
            source_ref=blank,
            source_fingerprint="0" * 64,
            converter="test-1.0",
        ),
        markdown_ref=blank,
        diagnostics_ref=await store.put_text("[]"),
        chunks=chunks,
    )


class SentenceSplitTest(unittest.TestCase):
    def test_splits_on_sentence_terminators(self) -> None:
        self.assertEqual([(0, 12), (13, 22)], split_sentences("Hello there. Next one."))

    def test_abbreviations_and_initials_do_not_end_a_sentence(self) -> None:
        text = "See Fig. 1 and cf. Yang et al. for detail. Done."
        spans = split_sentences(text)

        self.assertEqual(
            ["See Fig. 1 and cf. Yang et al. for detail.", "Done."],
            [text[start:end] for start, end in spans],
        )

    def test_lines_stay_separate_so_table_rows_are_not_merged(self) -> None:
        text = "| Method | Acc |\n| --- | --- |\n| A-RAG | 74.1 |"
        spans = split_sentences(text)

        self.assertEqual(
            ["| Method | Acc |", "| A-RAG | 74.1 |"],
            [text[start:end] for start, end in spans],
        )

    def test_decimals_do_not_split(self) -> None:
        spans = split_sentences("Accuracy reached 74.1 percent overall.")

        self.assertEqual(1, len(spans))

    def test_structural_lines_do_not_become_retrieval_units(self) -> None:
        text = (
            "> Section: Experiments / Ablation\n"
            "## Ablation Study\n"
            "$$\n"
            "Removing chunk read costs 3.1 points.\n"
            "| Variant | Acc |\n"
            "| :-- | --: |\n"
            "| w/o read | 71.0 |"
        )
        spans = split_sentences(text)

        self.assertEqual(
            [
                "Removing chunk read costs 3.1 points.",
                "| Variant | Acc |",
                "| w/o read | 71.0 |",
            ],
            [text[start:end] for start, end in spans],
        )

    def test_numeric_table_rows_survive_while_delimiters_do_not(self) -> None:
        self.assertTrue(is_indexable("| 74.1 | 66.2 |"))
        self.assertFalse(is_indexable("| --- | :---: |"))
        self.assertFalse(is_indexable("$$"))
        self.assertFalse(is_indexable("\\["))
        self.assertTrue(is_indexable("Gómez proposed the variant."))


class CorpusIndexTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))

    async def test_builds_entries_and_sentences_without_an_embedder(self) -> None:
        paper = await make_paper(
            self.store, "p1", "Paper One", ["First sentence. Second sentence."]
        )

        corpus_ref = await build_corpus_index(self.store, [paper])
        index = PaperCorpusIndex.model_validate_json(
            await self.store.get_text(corpus_ref)
        )

        self.assertIsNone(index.embedding_ref)
        self.assertEqual(1, len(index.entries))
        self.assertEqual("p1:c0", index.entries[0].chunk_id)
        self.assertEqual("Paper One", index.entries[0].title)
        self.assertEqual(2, len(index.sentences))
        self.assertEqual("First sentence.", index.sentence_text(0))

    async def test_sentence_ranges_stay_inside_their_owning_entry(self) -> None:
        paper = await make_paper(
            self.store, "p1", "Paper One", ["Alpha one. Alpha two.", "Beta one."]
        )

        corpus_ref = await build_corpus_index(self.store, [paper])
        index = PaperCorpusIndex.model_validate_json(
            await self.store.get_text(corpus_ref)
        )

        for position, sentence in enumerate(index.sentences):
            entry = index.entries[sentence.entry_index]
            self.assertIn(index.sentence_text(position), entry.text)
        self.assertEqual(0, index.entries[0].sentence_start)
        self.assertEqual(2, index.entries[0].sentence_end)
        self.assertEqual(2, index.entries[1].sentence_start)

    async def test_embedder_records_model_and_persists_one_vector_per_sentence(
        self,
    ) -> None:
        paper = await make_paper(
            self.store, "p1", "Paper One", ["Retrieval works. Grasping differs."]
        )
        embedder = FakeEmbedder(VOCABULARY)

        corpus_ref = await build_corpus_index(self.store, [paper], embedder)
        session = RetrievalSession()
        corpus = await session.load(self.store, corpus_ref)

        self.assertEqual("fake-embed-1", corpus.index.embedding_model)
        self.assertEqual(len(corpus.index.sentences), len(corpus.vectors))
        self.assertEqual([2], embedder.batches)


class SearchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    async def load(self, texts: list[str], embedder: FakeEmbedder | None = None):
        paper = await make_paper(self.store, "p1", "Paper One", texts)
        corpus_ref = await build_corpus_index(self.store, [paper], embedder)
        return await self.session.load(self.store, corpus_ref)

    async def test_longer_keywords_outrank_more_frequent_short_ones(self) -> None:
        corpus = await self.load(["RAG RAG RAG.", "Hierarchical retrieval."])

        hits = keyword_search(corpus, ["rag", "hierarchical retrieval"], 5)

        self.assertEqual(["p1:c1", "p1:c0"], [hit.chunk_id for hit in hits])
        self.assertEqual(22.0, hits[0].score)
        self.assertEqual(9.0, hits[1].score)

    async def test_keyword_snippet_holds_only_matching_sentences(self) -> None:
        corpus = await self.load(
            ["Entity confusion dominates the errors. Unrelated closing sentence."]
        )

        hits = keyword_search(corpus, ["entity confusion"], 5)

        self.assertEqual("Entity confusion dominates the errors.", hits[0].snippet)

    async def test_keyword_search_skips_chunks_without_any_match(self) -> None:
        corpus = await self.load(["Retrieval interfaces.", "Nothing relevant here."])

        hits = keyword_search(corpus, ["retrieval"], 5)

        self.assertEqual(["p1:c0"], [hit.chunk_id for hit in hits])

    async def test_semantic_search_scores_a_chunk_by_its_best_sentence(self) -> None:
        embedder = FakeEmbedder(VOCABULARY)
        corpus = await self.load(
            [
                "Hierarchical retrieval interfaces help the agent. "
                "Robotic grasping differs.",
                "Nothing relevant here.",
            ],
            embedder,
        )

        query = await embedder.embed(["retrieval"])
        hits = semantic_search(corpus, query[0], 5)

        self.assertEqual(["p1:c0"], [hit.chunk_id for hit in hits])
        self.assertAlmostEqual(1.0, hits[0].score)
        self.assertEqual(
            "Hierarchical retrieval interfaces help the agent.", hits[0].snippet
        )

    async def test_sparse_fragments_lose_to_a_self_contained_sentence(self) -> None:
        embedder = FakeEmbedder(VOCABULARY)
        corpus = await self.load(
            [
                "$r = $ retrieval",
                "Sentence-level retrieval keeps the matched evidence local.",
            ],
            embedder,
        )

        query = await embedder.embed(["retrieval"])
        hits = semantic_search(corpus, query[0], 5)

        # 两句的向量完全相同，排序只由自足度权重决定：1 个实词 vs 5 个以上
        self.assertEqual(["p1:c1", "p1:c0"], [hit.chunk_id for hit in hits])
        self.assertAlmostEqual(1.0, hits[0].score)
        self.assertAlmostEqual(0.2, hits[1].score)

    async def test_self_contained_weight_counts_words_not_characters(self) -> None:
        long_but_empty = "|  |  |  | 0.2 |  | 5.47 | 25.7 |  |  | 4.95 | 25.5 |  |"

        self.assertEqual(0.0, self_contained_weight(long_but_empty))
        self.assertEqual(
            1.0, self_contained_weight("Fully formed sentences read well.")
        )

    async def test_semantic_search_returns_nothing_without_vectors(self) -> None:
        corpus = await self.load(["Retrieval interfaces."])

        self.assertEqual([], semantic_search(corpus, [1.0, 0.0], 5))


class ChunkReadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()
        first = await make_paper(
            self.store, "p1", "Paper One", ["Alpha.", "Bravo.", "Charlie."]
        )
        second = await make_paper(self.store, "p2", "Paper Two", ["Delta."])
        corpus_ref = await build_corpus_index(self.store, [first, second])
        self.corpus = await self.session.load(self.store, corpus_ref)

    async def test_second_read_returns_a_notice_instead_of_the_text(self) -> None:
        first = read_chunks(self.corpus, self.session, ["p1:c1"], False)
        second = read_chunks(self.corpus, self.session, ["p1:c1"], False)

        self.assertEqual(("read", "Bravo."), (first[0].status, first[0].text))
        self.assertEqual(
            ("already_read", ALREADY_READ_NOTICE), (second[0].status, second[0].text)
        )

    async def test_adjacent_read_stops_at_the_paper_boundary(self) -> None:
        chunks = read_chunks(self.corpus, self.session, ["p1:c2"], True)

        self.assertEqual(["p1:c1", "p1:c2"], [chunk.chunk_id for chunk in chunks])

    async def test_unknown_chunk_id_is_reported_not_raised(self) -> None:
        chunks = read_chunks(self.corpus, self.session, ["p1:missing"], False)

        self.assertEqual("not_found", chunks[0].status)
        self.assertEqual("", chunks[0].text)


class ToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()
        self.embedder = FakeEmbedder(VOCABULARY)
        self.paper = await make_paper(
            self.store,
            "p1",
            "Paper One",
            ["Hierarchical retrieval interfaces help.", "Robotic grasping differs."],
        )

    async def test_registry_round_trip_shares_the_read_tracker(self) -> None:
        corpus_ref = await build_corpus_index(self.store, [self.paper], self.embedder)
        registry = ToolRegistry()
        registry.register(PaperKeywordSearchTool(self.store, self.session))
        registry.register(
            PaperSemanticSearchTool(self.store, self.embedder, self.session)
        )
        registry.register(PaperChunkReadTool(self.store, self.session))

        found = await registry.resolve("paper_keyword_search").ainvoke(
            context("paper_keyword_search"),
            corpus_ref=corpus_ref,
            keywords=["grasping"],
        )
        chunk_id = found.data["hits"][0]["chunk_id"]
        first = await registry.resolve("paper_chunk_read").ainvoke(
            context("paper_chunk_read"), corpus_ref=corpus_ref, chunk_ids=[chunk_id]
        )
        second = await registry.resolve("paper_semantic_search").ainvoke(
            context("paper_semantic_search"), corpus_ref=corpus_ref, query="grasping"
        )
        third = await registry.resolve("paper_chunk_read").ainvoke(
            context("paper_chunk_read"), corpus_ref=corpus_ref, chunk_ids=[chunk_id]
        )

        self.assertEqual("p1:c1", chunk_id)
        self.assertEqual("read", first.data["chunks"][0]["status"])
        self.assertEqual("p1:c1", second.data["hits"][0]["chunk_id"])
        self.assertEqual("already_read", third.data["chunks"][0]["status"])

    async def test_semantic_search_fails_loudly_on_a_corpus_without_vectors(
        self,
    ) -> None:
        corpus_ref = await build_corpus_index(self.store, [self.paper])
        tool = PaperSemanticSearchTool(self.store, self.embedder, self.session)

        result = await tool.ainvoke(
            context("paper_semantic_search"), corpus_ref=corpus_ref, query="retrieval"
        )

        self.assertFalse(result.success)
        self.assertIn("paper_keyword_search", result.error)

    async def test_semantic_search_rejects_a_mismatched_embedding_model(self) -> None:
        corpus_ref = await build_corpus_index(self.store, [self.paper], self.embedder)
        tool = PaperSemanticSearchTool(
            self.store, FakeEmbedder(VOCABULARY, model="other-embed-2"), self.session
        )

        result = await tool.ainvoke(
            context("paper_semantic_search"), corpus_ref=corpus_ref, query="retrieval"
        )

        self.assertFalse(result.success)
        self.assertIn("fake-embed-1", result.error)
        self.assertIn("other-embed-2", result.error)

    async def test_missing_corpus_ref_is_reported_as_a_failed_result(self) -> None:
        tool = PaperKeywordSearchTool(self.store, self.session)

        result = await tool.ainvoke(context("paper_keyword_search"), keywords=["x"])

        self.assertFalse(result.success)
        self.assertIn("corpus_ref", result.error)

    async def test_top_k_is_clamped_because_the_schema_is_not_enforced(self) -> None:
        corpus_ref = await build_corpus_index(self.store, [self.paper])
        tool = PaperKeywordSearchTool(self.store, self.session)

        result = await tool.ainvoke(
            context("paper_keyword_search"),
            corpus_ref=corpus_ref,
            keywords=["retrieval", "grasping"],
            k=0,
        )

        self.assertTrue(result.success)
        self.assertEqual(2, len(result.data["hits"]))


class ContextualSkeletonTest(unittest.IsolatedAsyncioTestCase):
    async def test_reserved_entry_point_refuses_instead_of_silently_passing_through(
        self,
    ) -> None:
        with self.assertRaises(NotImplementedError):
            await contextualize_entries([], "document", None)

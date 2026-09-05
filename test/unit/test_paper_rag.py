"""Tests for the A-RAG hierarchical retrieval interfaces."""

import asyncio
import json
import tempfile
import unittest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext
from athena.research.literature.paper_markdown.models import (
    PaperChunk,
    PaperContent,
    PaperProvenance,
    PaperVisual,
    SourceLocator,
)
from athena.research.literature.paper_rag.index import (
    SEMANTIC_MARGIN_THRESHOLD,
    SEMANTIC_PROBES,
    NonSemanticEmbedderError,
    build_corpus_index,
    display_math_close,
    is_indexable,
    paper_anchors,
    reference_edges,
    require_semantic_embedder,
    semantic_margin,
    split_sentences,
    title_matches,
    unpack_vectors,
)
from athena.research.literature.paper_rag.schemas import PaperCorpusIndex
from athena.research.literature.paper_rag.search import (
    RetrievalSession,
    corpus_overview,
    heading_variants,
    keyword_search,
    score_by_keywords,
    self_contained_weight,
    semantic_search,
)
from athena.research.literature.paper_rag.tool import (
    PaperChunkReadTool,
    PaperCitesTool,
    PaperCorpusOverviewTool,
    PaperKeywordSearchTool,
    PaperSectionSearchTool,
    PaperSemanticSearchTool,
    PaperVisualOfTool,
)
from athena.research.literature.paper_rag.traversal import (
    ALREADY_READ_NOTICE,
    citation_links,
    paper_namespace,
    read_chunks,
    section_search,
    visual_links,
)
from athena.research.literature.paper_scout.pool import title_key

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


class BagOfWordsEmbedder:
    """The lexical placeholder earlier runs had to use; kept as the negative case."""

    model = "bag-of-words"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vocabulary: dict[str, int] = {}
        for text in texts:
            for token in text.lower().split():
                vocabulary.setdefault(token.strip(".,"), len(vocabulary))
        vectors = []
        for text in texts:
            vector = [0.0] * max(len(vocabulary), 1)
            for token in text.lower().split():
                vector[vocabulary[token.strip(".,")]] += 1.0
            vectors.append(vector)
        return vectors


class SemanticStubEmbedder:
    """Places each probe anchor beside its paraphrase and away from the distractor."""

    model = "semantic-stub"

    def __init__(self) -> None:
        self.placement: dict[str, list[float]] = {}
        for index, (anchor, paraphrase, unrelated) in enumerate(SEMANTIC_PROBES):
            topic = [0.0] * (len(SEMANTIC_PROBES) + 1)
            topic[index] = 1.0
            self.placement[anchor] = list(topic)
            near = list(topic)
            near[-1] = 0.3
            self.placement[paraphrase] = near
            far = [0.0] * (len(SEMANTIC_PROBES) + 1)
            far[-1] = 1.0
            self.placement[unrelated] = far

    async def embed(self, texts: list[str]) -> list[list[float]]:
        width = len(SEMANTIC_PROBES) + 1
        return [self.placement.get(text, [0.0] * width) for text in texts]


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


CITED_TITLE = "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"


async def make_citing_pair(store: LocalArtifactStore) -> list[PaperContent]:
    """Two papers where the second cites the first, with a real bibliography chunk."""
    cited = await make_paper(store, "p-cited", CITED_TITLE, ["We introduce RAG."])
    cited.chunks[0].kind = "abstract"
    citing = await make_paper(
        store,
        "p-citing",
        "Few-shot Learning with Retrieval Augmented Language Models",
        ["We build on retrieval augmentation [@lewis2020].", "Unrelated closing text."],
    )
    citing.chunks[0].citation_keys = ["lewis2020"]
    reference = (
        "## References\n\n- [@lewis2020] Patrick Lewis, Ethan Perez, and others. "
        f"{CITED_TITLE}. NeurIPS 2020."
    )
    outside = (
        "## References\n\n- [@vaswani2017] Ashish Vaswani and others. "
        "Attention Is All You Need. NeurIPS 2017."
    )
    for position, text in enumerate((reference, outside), start=len(citing.chunks)):
        citing.chunks.append(
            PaperChunk(
                chunk_id=f"b{position}",
                kind="bibliography",
                content_ref=await store.put_text(text),
                heading_path=["References"],
                char_start=0,
                char_end=len(text),
                token_estimate=len(text) // 4,
            )
        )
    return [cited, citing]


async def make_illustrated_paper(
    store: LocalArtifactStore, interpretation_status: str
) -> PaperContent:
    """Persist a paper whose first chunk discusses a figure, linked in both directions."""
    paper = await make_paper(
        store,
        "p1",
        "Paper One",
        ["Retrieval accuracy is plotted in Figure 1.", "Unrelated closing chunk."],
    )
    paper.chunks[0].visual_ids = ["figure-1"]
    paper.visuals = [
        PaperVisual(
            visual_id="figure-1",
            kind="figure",
            heading_path=["Method"],
            chunk_ids=["c0"],
            label="fig:acc",
            caption="Retrieval accuracy by variant.",
            structured_text_ref=await store.put_text("accuracy plot"),
            interpretation_ref=await store.put_text("{}"),
            interpretation_status=interpretation_status,
            search_text_ref=await store.put_text(
                "Figure 1 plots retrieval accuracy for each variant across datasets."
            ),
            locator=SourceLocator(
                source_kind="tex", file="main.tex", line_start=1, line_end=1
            ),
        )
    ]
    return paper


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

    def test_sentence_broken_by_display_math_is_rejoined(self) -> None:
        text = (
            "We obtain the identity\n"
            "$$\n"
            "\\begin{aligned}\n"
            "\\mathcal F_{n,m,k}\n"
            "&= \\frac{\\Gamma(m/2)}{\\Gamma(m)}\n"
            "\\end{aligned}\n"
            "$$\n"
            "and conclude the proof.\n"
            "A separate sentence stands alone."
        )
        spans = split_sentences(text)
        blocks = [text[start:end] for start, end in spans]

        self.assertEqual(2, len(blocks))
        self.assertTrue(blocks[0].startswith("We obtain the identity\n$$"))
        self.assertTrue(blocks[0].endswith("and conclude the proof."))
        self.assertIn("\\end{aligned}", blocks[0])
        self.assertEqual("A separate sentence stands alone.", blocks[1])

    def test_chained_equations_join_into_one_unit(self) -> None:
        text = "\\[\nQ_\\lambda(u)=S_{\\lambda,k}(M)\n\\]\nand\n\\[\nR(u)=0\n\\]"
        spans = split_sentences(text)

        self.assertEqual([(0, len(text))], spans)

    def test_a_terminated_sentence_before_math_is_not_absorbed(self) -> None:
        text = "The setup is fixed.\n$$\nx = 1 + y\n$$"
        spans = split_sentences(text)

        self.assertEqual(
            ["The setup is fixed.", "$$\nx = 1 + y\n$$"],
            [text[start:end] for start, end in spans],
        )

    def test_new_sentence_after_math_is_not_absorbed(self) -> None:
        text = "We define\n$$\nf(x) = 0\n$$\nThe proof is immediate."
        spans = split_sentences(text)
        blocks = [text[start:end] for start, end in spans]

        self.assertEqual(2, len(blocks))
        self.assertTrue(blocks[0].startswith("We define\n$$"))
        self.assertEqual("The proof is immediate.", blocks[1])

    def test_table_rows_are_never_joined_to_each_other(self) -> None:
        text = "| Method | Acc |\n| --- | --- |\n| A-RAG | 74.1 |\n| Naive | 66.2 |"
        spans = split_sentences(text)

        self.assertEqual(
            ["| Method | Acc |", "| A-RAG | 74.1 |", "| Naive | 66.2 |"],
            [text[start:end] for start, end in spans],
        )

    def test_bare_math_environments_are_kept_whole(self) -> None:
        text = (
            "First we state the identity.\n"
            "\\begin{equation}\n"
            "E = mc^2\n"
            "\\end{equation}\n"
            "That is the mass energy relation.\n"
            "\\begin{align*}\n"
            "a &= b \\\\\n"
            "c &= d\n"
            "\\end{align*}\n"
            "Done."
        )
        spans = split_sentences(text)
        blocks = [text[start:end] for start, end in spans]

        self.assertEqual(5, len(blocks))
        self.assertEqual("\\begin{equation}\nE = mc^2\n\\end{equation}", blocks[1])
        self.assertTrue(blocks[3].startswith("\\begin{align*}"))
        self.assertTrue(blocks[3].endswith("\\end{align*}"))

    def test_inner_only_environments_do_not_open_a_block(self) -> None:
        # aligned 只能嵌套在数学模式内，顶层出现时不应吞掉后面的正文
        self.assertIsNone(display_math_close("\\begin{aligned}"))
        self.assertIsNone(display_math_close("\\begin{cases}"))
        self.assertEqual("\\end{equation}", display_math_close("\\begin{equation}"))
        self.assertEqual("\\end{gather*}", display_math_close("  \\begin{gather*}  "))
        self.assertEqual("$$", display_math_close("$$"))

    def test_unclosed_math_fence_does_not_swallow_the_rest_of_the_chunk(self) -> None:
        text = "$$\nx = 1\nA sentence that must stay retrievable on its own."
        spans = split_sentences(text)

        self.assertEqual(
            ["A sentence that must stay retrievable on its own."],
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

    async def test_text_and_visual_units_link_to_each_other(self) -> None:
        paper = await make_illustrated_paper(self.store, "interpreted")

        corpus_ref = await build_corpus_index(self.store, [paper])
        index = PaperCorpusIndex.model_validate_json(
            await self.store.get_text(corpus_ref)
        )
        by_id = {entry.chunk_id: entry for entry in index.entries}

        self.assertIn("p1:figure-1", by_id)
        self.assertEqual(["p1:figure-1"], by_id["p1:c0"].visual_ids)
        self.assertEqual(["p1:c0"], by_id["p1:figure-1"].visual_ids)
        self.assertEqual([], by_id["p1:c1"].visual_ids)

    async def test_links_to_uninterpreted_visuals_are_dropped(self) -> None:
        paper = await make_illustrated_paper(self.store, "unavailable")

        corpus_ref = await build_corpus_index(self.store, [paper])
        index = PaperCorpusIndex.model_validate_json(
            await self.store.get_text(corpus_ref)
        )
        by_id = {entry.chunk_id: entry for entry in index.entries}

        # 视觉单元没进语料，指向它的链接必须一并剔除，否则 Agent 会读到 not_found
        self.assertNotIn("p1:figure-1", by_id)
        self.assertEqual([], by_id["p1:c0"].visual_ids)

    async def test_embedder_records_model_and_persists_one_vector_per_sentence(
        self,
    ) -> None:
        paper = await make_paper(
            self.store, "p1", "Paper One", ["Retrieval works. Grasping differs."]
        )
        embedder = FakeEmbedder(VOCABULARY)

        corpus_ref = await build_corpus_index(self.store, [paper], embedder)
        session = RetrievalSession()
        corpus = await session.load(self.store, corpus_ref, vectors=True)

        self.assertEqual("fake-embed-1", corpus.index.embedding_model)
        self.assertEqual("float32", corpus.index.embedding_format)
        self.assertEqual(len(corpus.index.sentences), len(corpus.vectors))
        self.assertEqual([2], embedder.batches)

    async def test_only_semantic_search_pays_for_loading_the_sentence_vectors(
        self,
    ) -> None:
        """向量是语料里最贵的部分；不需要它的算子不该被迫装载它。"""
        paper = await make_paper(
            self.store, "p1", "Paper One", ["Retrieval works. Grasping differs."]
        )
        corpus_ref = await build_corpus_index(
            self.store, [paper], FakeEmbedder(VOCABULARY)
        )
        session = RetrievalSession()

        without = await session.load(self.store, corpus_ref)
        self.assertFalse(without.has_vectors())

        withvectors = await session.load(self.store, corpus_ref, vectors=True)
        self.assertTrue(withvectors.has_vectors())
        self.assertIs(without, withvectors)

    async def test_legacy_json_vectors_still_load_into_the_same_matrix(self) -> None:
        """1.0 语料的向量在磁盘上是 JSON 文本；换格式不能把已有语料变成砖头。"""
        paper = await make_paper(self.store, "p1", "Paper One", ["Retrieval works."])
        corpus_ref = await build_corpus_index(
            self.store, [paper], FakeEmbedder(VOCABULARY)
        )
        index = PaperCorpusIndex.model_validate_json(
            await self.store.get_text(corpus_ref)
        )
        vectors = unpack_vectors(await self.store.get_bytes(index.embedding_ref))
        legacy = index.model_copy(
            update={
                "schema_version": "1.0",
                "embedding_format": "json",
                "embedding_ref": await self.store.put_text(
                    json.dumps(vectors.tolist())
                ),
            }
        )
        legacy_ref = await self.store.put_text(legacy.model_dump_json())

        corpus = await RetrievalSession().load(self.store, legacy_ref, vectors=True)

        self.assertEqual(vectors.shape, corpus.vectors.shape)


class SearchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    async def load(self, texts: list[str], embedder: FakeEmbedder | None = None):
        paper = await make_paper(self.store, "p1", "Paper One", texts)
        corpus_ref = await build_corpus_index(self.store, [paper], embedder)
        return await self.session.load(
            self.store, corpus_ref, vectors=embedder is not None
        )

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

    async def test_search_and_read_carry_the_link_to_the_figure(self) -> None:
        store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        session = RetrievalSession()
        paper = await make_illustrated_paper(store, "interpreted")
        corpus_ref = await build_corpus_index(store, [paper])
        corpus = await session.load(store, corpus_ref)

        hits = keyword_search(corpus, ["retrieval accuracy"], 5)
        target = next(hit for hit in hits if hit.chunk_id == "p1:c0")
        figure = read_chunks(corpus, session, target.visual_ids, False)

        self.assertEqual(["p1:figure-1"], target.visual_ids)
        self.assertEqual("read", figure[0].status)
        self.assertIn("plots retrieval accuracy", figure[0].text)
        self.assertEqual(["p1:c0"], figure[0].visual_ids)

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


class BibliographyAsCitationEdgeTest(unittest.IsolatedAsyncioTestCase):
    """References carry more value as graph edges than as retrievable passages.

    SciRAG (EACL 2026) calls indexing them as plain text a superficial use of the
    citation structure; here they leave the retrievable set and reappear as links
    from the citing chunk to the cited paper.
    """

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp())
        self.papers = await make_citing_pair(self.store)

    async def load(self, **kwargs) -> PaperCorpusIndex:
        ref = await build_corpus_index(self.store, self.papers, **kwargs)
        return PaperCorpusIndex.model_validate_json(await self.store.get_text(ref))

    async def test_bibliography_is_not_a_retrieval_unit_by_default(self) -> None:
        index = await self.load()
        self.assertNotIn("bibliography", {entry.kind for entry in index.entries})

    async def test_bibliography_can_be_restored_for_comparison(self) -> None:
        index = await self.load(index_bibliography=True)
        kinds = [entry.kind for entry in index.entries]
        self.assertEqual(2, kinds.count("bibliography"))

    async def test_citing_chunk_links_to_the_cited_paper(self) -> None:
        index = await self.load()
        citing = next(entry for entry in index.entries if "[@lewis2020]" in entry.text)
        target = next(entry for entry in index.entries if entry.paper_id == "p-cited")
        self.assertIn(target.chunk_id, citing.cited_ids)

    async def test_the_link_lands_on_the_abstract_of_the_cited_paper(self) -> None:
        index = await self.load()
        citing = next(entry for entry in index.entries if "[@lewis2020]" in entry.text)
        landed = next(
            entry for entry in index.entries if entry.chunk_id == citing.cited_ids[0]
        )
        self.assertEqual("abstract", landed.kind)

    async def test_references_outside_the_corpus_create_no_edge(self) -> None:
        index = await self.load()
        for entry in index.entries:
            self.assertTrue(
                all(
                    item in {e.chunk_id for e in index.entries}
                    for item in entry.cited_ids
                )
            )
        citing = next(entry for entry in index.entries if "[@lewis2020]" in entry.text)
        self.assertEqual(1, len(citing.cited_ids))

    async def test_dropping_bibliography_shrinks_the_sentence_index(self) -> None:
        without = await self.load()
        with_bibliography = await self.load(index_bibliography=True)
        self.assertLess(len(without.sentences), len(with_bibliography.sentences))
        self.assertLess(len(without.entries), len(with_bibliography.entries))

    async def test_a_reference_that_misspells_the_title_still_links(self) -> None:
        """真实语料里作者拼错自己题目的引用不止一处；整题包含对一个字符全或无。"""
        typo = self.papers[1].chunks[2]
        typo.content_ref = await self.store.put_text(
            "## References\n\n- [@lewis2020] Patrick Lewis and others. "
            "Retrieval-Augmented Generation for Knowledge-Intensiv NLP Tasks. "
            "NeurIPS 2020."
        )
        index = await self.load()

        citing = next(entry for entry in index.entries if "[@lewis2020]" in entry.text)
        target = next(entry for entry in index.entries if entry.paper_id == "p-cited")
        self.assertIn(target.chunk_id, citing.cited_ids)

    async def test_a_short_generic_title_never_matches_loosely(self) -> None:
        """放宽匹配的唯一风险来自短标题；40 字符的连续段长度让它们结构上够不到。

        标题规范化后只有 29 字符，因此**只有**整题包含能让它命中；一个字符的出入就该
        判定为不同论文，而不是像长标题那样被连续段规则救回来。
        """
        short = title_key("Enhanced Cost-sensitive Ensemble")
        near_miss = title_key(
            "Raza A and others. Novel class probability features for optimizing "
            "network attack detection with enhance cost sensitive ensembles. "
            "IEEE Access 2023."
        )
        exact = title_key(
            "Raza A and others. Enhanced Cost-sensitive Ensemble. IEEE Access 2023."
        )

        self.assertFalse(title_matches(short, near_miss))
        self.assertTrue(title_matches(short, exact))


class CorpusOverviewTest(unittest.IsolatedAsyncioTestCase):
    """拿到 corpus_ref 之后的第一步，也是 ``sources`` 合法取值的唯一来源。"""

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()
        first = await make_paper(
            self.store, "p1", "Paper One", ["We introduce A.", "Ablation on A."]
        )
        first.chunks[0].kind = "abstract"
        first.chunks[1].heading_path = ["Ablation Study"]
        second = await make_paper(self.store, "p2", "Paper Two", ["We introduce B."])
        corpus_ref = await build_corpus_index(self.store, [first, second])
        self.corpus_ref = corpus_ref
        self.corpus = await self.session.load(self.store, corpus_ref)

    async def test_every_paper_is_listed_with_the_key_to_cite(self) -> None:
        overview = corpus_overview(self.corpus, [], 30)

        self.assertEqual(2, overview.papers)
        self.assertEqual(["p1", "p2"], [item.paper_id for item in overview.summaries])
        self.assertEqual(
            ["Paper One", "Paper Two"], [i.title for i in overview.summaries]
        )

    async def test_the_anchor_is_the_abstract_when_the_paper_has_one(self) -> None:
        overview = corpus_overview(self.corpus, [], 30)

        first = overview.summaries[0]
        self.assertEqual("p1:c0", first.anchor_chunk_id)
        self.assertEqual("We introduce A.", first.abstract)
        self.assertEqual(2, first.chunks)

    async def test_it_reports_the_section_names_that_actually_exist(self) -> None:
        """章节名靠猜是这个语料上最容易空手而归的一步。"""
        overview = corpus_overview(self.corpus, [], 30)

        self.assertEqual(["Method", "Ablation Study"], overview.summaries[0].sections)

    async def test_paper_ids_narrow_the_listing_without_changing_the_totals(
        self,
    ) -> None:
        overview = corpus_overview(self.corpus, ["p2"], 30)

        self.assertEqual(2, overview.papers)
        self.assertEqual(["p2"], [item.paper_id for item in overview.summaries])

    async def test_the_tool_reports_whether_semantic_search_is_available(self) -> None:
        tool = PaperCorpusOverviewTool(self.store, self.session)

        result = await tool.ainvoke(
            context("paper_corpus_overview"), corpus_ref=self.corpus_ref
        )

        self.assertTrue(result.success)
        self.assertFalse(result.data["semantic_search"])
        self.assertEqual(2, len(result.data["summaries"]))

    async def test_the_overview_never_loads_the_sentence_vectors(self) -> None:
        """一次"里面有什么"的问询不该把语料里最贵的部分拖进内存。"""
        session = RetrievalSession()
        paper = await make_paper(self.store, "p3", "Paper Three", ["Retrieval works."])
        corpus_ref = await build_corpus_index(
            self.store, [paper], FakeEmbedder(VOCABULARY)
        )
        tool = PaperCorpusOverviewTool(self.store, session)

        await tool.ainvoke(context("paper_corpus_overview"), corpus_ref=corpus_ref)

        self.assertFalse((await session.load(self.store, corpus_ref)).has_vectors())


class SectionAliasTest(unittest.IsolatedAsyncioTestCase):
    """论文对同一部分的叫法不统一；按字面匹配会让跨论文对比这个算子存在的理由落空。"""

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        papers = []
        for index, heading in enumerate(
            ("Limitations", "Threats to Validity", "Shortcomings"), start=1
        ):
            paper = await make_paper(
                self.store, f"p{index}", f"Paper {index}", [f"Caveat {index}."]
            )
            paper.chunks[0].heading_path = [heading]
            papers.append(paper)
        corpus_ref = await build_corpus_index(self.store, papers)
        self.corpus = await RetrievalSession().load(self.store, corpus_ref)

    def test_one_heading_reaches_every_paper_that_uses_a_synonym(self) -> None:
        hits = section_search(self.corpus, "Limitations", [], 10)

        self.assertEqual({"p1", "p2", "p3"}, {hit.paper_id for hit in hits})

    def test_an_unregistered_heading_keeps_its_exact_meaning(self) -> None:
        self.assertEqual(("appendix",), heading_variants("Appendix"))
        self.assertEqual([], section_search(self.corpus, "Appendix", [], 10))

    def test_the_query_itself_always_stays_in_the_variants(self) -> None:
        """别名只放宽范围：登记过的组也不能把用户写的那个标题挤出去。"""
        self.assertIn(
            "limitations and future work",
            heading_variants("Limitations and Future Work"),
        )


class SemanticEmbedderGuardTest(unittest.IsolatedAsyncioTestCase):
    """A lexical stand-in builds a corpus that looks fine and retrieves noise.

    Measured on the same corpus, lexical vs neural encoding differ by 8.7x in MRR
    (0.048 vs 0.417) with R@1 of 0.025, so an accidental injection is not a small
    regression. The probe pairs share no content words between anchor and
    paraphrase, which is exactly what a bag-of-words encoder cannot bridge.
    """

    async def test_a_lexical_encoder_is_rejected(self) -> None:
        with self.assertRaises(NonSemanticEmbedderError):
            await require_semantic_embedder(BagOfWordsEmbedder())

    async def test_the_rejection_names_the_model_and_the_threshold(self) -> None:
        with self.assertRaises(NonSemanticEmbedderError) as caught:
            await require_semantic_embedder(BagOfWordsEmbedder())
        message = str(caught.exception)
        self.assertIn("bag-of-words", message)
        self.assertIn(str(SEMANTIC_MARGIN_THRESHOLD), message)

    async def test_a_semantic_encoder_is_accepted(self) -> None:
        margin = await require_semantic_embedder(SemanticStubEmbedder())
        self.assertGreaterEqual(margin, SEMANTIC_MARGIN_THRESHOLD)

    async def test_margin_is_reported_without_raising(self) -> None:
        self.assertLess(await semantic_margin(BagOfWordsEmbedder()), 0.25)
        self.assertGreater(await semantic_margin(SemanticStubEmbedder()), 0.25)


class TypedEdgeTest(unittest.IsolatedAsyncioTestCase):
    """Text/visual links and citation links are different relations.

    Merged into one untyped list an agent can only follow them blindly; separated,
    "look at the figure this passage discusses" and "find who cites this paper" are
    two nameable actions with different answers.
    """

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    async def load_illustrated(self):
        paper = await make_illustrated_paper(self.store, "interpreted")
        return await self.session.load(
            self.store, await build_corpus_index(self.store, [paper])
        )

    async def load_citing(self):
        return await self.session.load(
            self.store,
            await build_corpus_index(self.store, await make_citing_pair(self.store)),
        )

    async def test_the_two_relations_live_in_separate_fields(self) -> None:
        corpus = await self.load_citing()
        citing = next(
            entry for entry in corpus.index.entries if "[@lewis2020]" in entry.text
        )

        self.assertEqual([], citing.visual_ids)
        self.assertEqual(1, len(citing.cited_ids))

    async def test_visual_links_walk_in_both_directions(self) -> None:
        corpus = await self.load_illustrated()

        forward = visual_links(corpus, ["p1:c0"])
        backward = visual_links(corpus, ["p1:figure-1"])

        self.assertEqual(["p1:figure-1"], [hit.chunk_id for hit in forward])
        self.assertEqual(["p1:c0"], [hit.chunk_id for hit in backward])

    async def test_traversal_hits_carry_an_orienting_snippet(self) -> None:
        corpus = await self.load_illustrated()

        hit = visual_links(corpus, ["p1:c0"])[0]

        self.assertIn("Figure 1", hit.snippet)
        self.assertEqual("visual:figure", hit.kind)

    async def test_unknown_and_repeated_ids_are_skipped_not_reported(self) -> None:
        corpus = await self.load_illustrated()

        hits = visual_links(corpus, ["p1:c0", "p1:c0", "p1:missing"])

        self.assertEqual(["p1:figure-1"], [hit.chunk_id for hit in hits])

    async def test_forward_citation_reaches_the_cited_paper(self) -> None:
        corpus = await self.load_citing()
        citing = next(
            entry for entry in corpus.index.entries if "[@lewis2020]" in entry.text
        )

        hits = citation_links(corpus, [citing.chunk_id], "cites")

        self.assertEqual(1, len(hits))
        self.assertEqual("p-cited", hits[0].paper_id)

    async def test_reverse_citation_finds_who_cites_the_paper(self) -> None:
        corpus = await self.load_citing()

        hits = citation_links(corpus, ["p-cited:c0"], "cited_by")

        self.assertEqual(1, len(hits))
        self.assertEqual("p-citing", hits[0].paper_id)
        self.assertIn("[@lewis2020]", hits[0].snippet)

    async def test_reverse_citation_accepts_any_chunk_of_the_cited_paper(self) -> None:
        """Edges land on the paper anchor, so a mid-body id must still resolve."""
        papers = await make_citing_pair(self.store)
        papers[0].chunks.append(
            PaperChunk(
                chunk_id="c9",
                kind="paragraph",
                content_ref=await self.store.put_text("A later section of the paper."),
                heading_path=["Method"],
                char_start=0,
                char_end=30,
                token_estimate=8,
            )
        )
        corpus = await self.session.load(
            self.store, await build_corpus_index(self.store, papers)
        )

        hits = citation_links(corpus, ["p-cited:c9"], "cited_by")

        self.assertEqual(["p-citing"], [hit.paper_id for hit in hits])


class SectionSearchTest(unittest.IsolatedAsyncioTestCase):
    """Contrary evidence sits in a comparable section of a different paper.

    Semantic search ranks text that agrees with the query highest because agreement
    is what similarity measures; a structural entry point does not have that bias.
    """

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    async def corpus(self):
        papers = []
        for name in ("pa", "pb"):
            paper = await make_paper(
                self.store, name, f"Paper {name}", ["Method text.", "Limits text."]
            )
            paper.chunks[1].heading_path = ["Limitations"]
            papers.append(paper)
        return await self.session.load(
            self.store, await build_corpus_index(self.store, papers)
        )

    async def test_a_heading_spans_every_paper_by_default(self) -> None:
        hits = section_search(await self.corpus(), "limitations", [], 10)

        self.assertEqual({"pa", "pb"}, {hit.paper_id for hit in hits})

    async def test_matching_is_case_insensitive_and_partial(self) -> None:
        hits = section_search(await self.corpus(), "LIMIT", [], 10)

        self.assertEqual(2, len(hits))

    async def test_paper_ids_restrict_the_slice(self) -> None:
        hits = section_search(await self.corpus(), "limitations", ["pa"], 10)

        self.assertEqual(["pa"], [hit.paper_id for hit in hits])

    async def test_an_unknown_heading_returns_nothing_rather_than_guessing(
        self,
    ) -> None:
        self.assertEqual([], section_search(await self.corpus(), "appendix", [], 10))


class TypedOperatorToolTest(unittest.IsolatedAsyncioTestCase):
    """The operator boundary: which tool the agent picks is the routing decision."""

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()
        self.embedder = FakeEmbedder(VOCABULARY)

    async def test_visual_of_tool_round_trips_through_the_registry(self) -> None:
        paper = await make_illustrated_paper(self.store, "interpreted")
        corpus_ref = await build_corpus_index(self.store, [paper])
        registry = ToolRegistry()
        registry.register(PaperVisualOfTool(self.store, self.session))

        result = await registry.resolve("paper_visual_of").ainvoke(
            context("paper_visual_of"), corpus_ref=corpus_ref, chunk_ids=["p1:c0"]
        )

        self.assertTrue(result.success)
        self.assertEqual("p1:figure-1", result.data["hits"][0]["chunk_id"])

    async def test_cites_tool_reports_the_direction_it_walked(self) -> None:
        corpus_ref = await build_corpus_index(
            self.store, await make_citing_pair(self.store)
        )
        tool = PaperCitesTool(self.store, self.session)

        result = await tool.ainvoke(
            context("paper_cites"),
            corpus_ref=corpus_ref,
            chunk_ids=["p-cited:c0"],
            direction="cited_by",
        )

        self.assertEqual("cited_by", result.data["direction"])
        self.assertEqual("p-citing", result.data["hits"][0]["paper_id"])

    async def test_cites_tool_rejects_an_unknown_direction(self) -> None:
        corpus_ref = await build_corpus_index(
            self.store, await make_citing_pair(self.store)
        )
        tool = PaperCitesTool(self.store, self.session)

        result = await tool.ainvoke(
            context("paper_cites"),
            corpus_ref=corpus_ref,
            chunk_ids=["p-cited:c0"],
            direction="sideways",
        )

        self.assertFalse(result.success)
        self.assertIn("direction", result.error)

    async def test_section_tool_requires_a_heading(self) -> None:
        corpus_ref = await build_corpus_index(
            self.store,
            [await make_paper(self.store, "p1", "Paper One", ["Plain text."])],
        )
        tool = PaperSectionSearchTool(self.store, self.session)

        result = await tool.ainvoke(
            context("paper_section_search"), corpus_ref=corpus_ref, heading="  "
        )

        self.assertFalse(result.success)
        self.assertIn("heading", result.error)


class NamespacedIdTest(unittest.IsolatedAsyncioTestCase):
    """Real paper keys carry a prefix, so the namespace is not the first segment.

    A left split turns every arxiv paper into the namespace "arxiv", which made
    reverse citation return the citing chunks of the whole corpus instead of the
    ones citing the paper asked about. Caught by a live run, not by the fixtures,
    because the fixtures used bare ids.
    """

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    def test_namespace_keeps_the_prefixed_paper_key(self) -> None:
        self.assertEqual("arxiv:1706.03762", paper_namespace("arxiv:1706.03762:c0"))
        self.assertEqual("doi:10.1145/x", paper_namespace("doi:10.1145/x:chunk-a"))
        self.assertEqual("p1", paper_namespace("p1:c0"))

    async def test_reverse_citation_does_not_leak_across_papers(self) -> None:
        """Two prefixed papers citing a third: asking about one must not return both."""
        cited = await make_paper(
            self.store, "arxiv:1000.0001", CITED_TITLE, ["We introduce RAG."]
        )
        cited.chunks[0].kind = "abstract"
        other = await make_paper(
            self.store,
            "arxiv:2000.0002",
            "An Unrelated Paper About Grasping Objects",
            ["Nothing to do with retrieval."],
        )
        papers = [cited, other]
        for index, citing_id in enumerate(("arxiv:3000.0003", "arxiv:4000.0004")):
            citing = await make_paper(
                self.store,
                citing_id,
                f"Citing Paper Number {index} With A Sufficiently Long Title",
                [f"We build on retrieval augmentation [@lewis{index}]."],
            )
            citing.chunks[0].citation_keys = [f"lewis{index}"]
            reference = (
                f"## References\n\n- [@lewis{index}] Patrick Lewis and others. "
                f"{CITED_TITLE}. NeurIPS 2020."
            )
            citing.chunks.append(
                PaperChunk(
                    chunk_id="b1",
                    kind="bibliography",
                    content_ref=await self.store.put_text(reference),
                    heading_path=["References"],
                    char_start=0,
                    char_end=len(reference),
                    token_estimate=len(reference) // 4,
                )
            )
            papers.append(citing)
        corpus = await self.session.load(
            self.store, await build_corpus_index(self.store, papers)
        )

        hits = citation_links(corpus, ["arxiv:1000.0001:c0"], "cited_by")
        empty = citation_links(corpus, ["arxiv:2000.0002:c0"], "cited_by")

        self.assertEqual(
            {"arxiv:3000.0003", "arxiv:4000.0004"}, {hit.paper_id for hit in hits}
        )
        self.assertEqual([], empty)


class SectionRoundRobinTest(unittest.IsolatedAsyncioTestCase):
    """The section operator exists to compare papers, so one paper must not fill it.

    A live run returned ten Ablation chunks drawn from a single paper while nine
    other papers had an Ablation section, which defeats the point of the operator.
    """

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    async def corpus(self, heavy: int, light: int):
        papers = [
            await make_paper(
                self.store,
                "heavy",
                "Heavy Paper",
                [f"Ablation detail number {index}." for index in range(heavy)],
            )
        ]
        for index in range(light):
            papers.append(
                await make_paper(
                    self.store,
                    f"light{index}",
                    f"Light Paper {index}",
                    ["A single ablation paragraph."],
                )
            )
        for paper in papers:
            for chunk in paper.chunks:
                chunk.heading_path = ["Ablation"]
        return await self.session.load(
            self.store, await build_corpus_index(self.store, papers)
        )

    async def test_every_paper_gets_a_slot_before_any_gets_a_second(self) -> None:
        hits = section_search(await self.corpus(heavy=20, light=4), "ablation", [], 5)

        self.assertEqual(5, len(hits))
        self.assertEqual(5, len({hit.paper_id for hit in hits}))

    async def test_a_deeper_budget_returns_to_the_richest_paper(self) -> None:
        """Three papers, budget six: one each, then the surplus goes where it exists."""
        hits = section_search(await self.corpus(heavy=20, light=2), "ablation", [], 6)

        counts: dict[str, int] = {}
        for hit in hits:
            counts[hit.paper_id] = counts.get(hit.paper_id, 0) + 1

        self.assertEqual({"heavy", "light0", "light1"}, set(counts))
        self.assertEqual(1, counts["light0"])
        self.assertEqual(1, counts["light1"])
        self.assertEqual(4, counts["heavy"])
        self.assertEqual({"heavy", "light0", "light1"}, {h.paper_id for h in hits[:3]})

    async def test_restricting_to_one_paper_still_returns_its_chunks(self) -> None:
        hits = section_search(
            await self.corpus(heavy=8, light=3), "ablation", ["heavy"], 5
        )

        self.assertEqual({"heavy"}, {hit.paper_id for hit in hits})
        self.assertEqual(5, len(hits))


class KeywordRoundRobinTest(unittest.IsolatedAsyncioTestCase):
    """名额按论文轮转，否则一篇高频使用该词的论文会吃光结果。

    真实跑测（44 篇语料，2026-08-16）：query "false positive rate range /
    specificity / restricted" 命中 67 个 chunk、来自 17 篇论文，而按全局得分直排时
    10 个名额有 9 个属于同一篇临床论文——它反复把 specificity 当指标名用。真正该出的
    论文最高分 chunk 排在全局第 17 位，正好落在 k=10 之外。
    """

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    async def corpus(self, heavy: int, light: int):
        papers = [
            await make_paper(
                self.store,
                "heavy",
                "Heavy Paper",
                [
                    f"Specificity is reported again, run {index}."
                    for index in range(heavy)
                ],
            )
        ]
        for index in range(light):
            papers.append(
                await make_paper(
                    self.store,
                    f"light{index}",
                    f"Light Paper {index}",
                    ["Specificity bounds the partial area under the curve."],
                )
            )
        return await self.session.load(
            self.store, await build_corpus_index(self.store, papers)
        )

    async def test_one_prolific_paper_does_not_fill_every_slot(self) -> None:
        hits = keyword_search(await self.corpus(heavy=20, light=4), ["specificity"], 5)

        self.assertEqual(5, len(hits))
        self.assertEqual(5, len({hit.paper_id for hit in hits}))

    async def test_surplus_slots_still_go_to_the_richest_paper(self) -> None:
        hits = keyword_search(await self.corpus(heavy=20, light=2), ["specificity"], 6)

        counts: dict[str, int] = {}
        for hit in hits:
            counts[hit.paper_id] = counts.get(hit.paper_id, 0) + 1

        self.assertEqual({"heavy", "light0", "light1"}, set(counts))
        self.assertEqual(4, counts["heavy"])
        self.assertEqual({"heavy", "light0", "light1"}, {h.paper_id for h in hits[:3]})


class KeywordTokenFallbackTest(unittest.IsolatedAsyncioTestCase):
    """短语一条都不中时退到词级重试一次。

    多词关键词按字面子串匹配，实测 27 条自然多词关键词里 7 条（26%）返回空，而这 7
    条拆成单词后全部有结果。对 Agent 而言"语料里没有"与"你的措辞没逐字出现"是两回
    事，当前接口把后者伪装成前者。
    """

    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="paper_rag_"))
        self.session = RetrievalSession()

    async def load(self, texts: list[str]):
        paper = await make_paper(self.store, "p1", "Paper One", texts)
        return await self.session.load(
            self.store, await build_corpus_index(self.store, [paper])
        )

    async def test_a_phrase_that_never_appears_verbatim_falls_back_to_tokens(
        self,
    ) -> None:
        corpus = await self.load(["The Wasserstein metric bounds the ball radius."])

        # 短语本身逐字不存在——正文里 Wasserstein 与 ball 相隔数词
        self.assertEqual([], score_by_keywords(corpus, ["wasserstein ball"]))

        hits = keyword_search(corpus, ["wasserstein ball"], 5)

        self.assertEqual(1, len(hits))
        self.assertIn("Wasserstein", hits[0].snippet)

    async def test_an_exact_phrase_match_never_triggers_the_fallback(self) -> None:
        """短语命中时不得退化成词级，否则精确检索会被拆散成一堆泛词命中。"""
        corpus = await self.load(
            ["Partial AUC is optimized here.", "The area under the curve is reported."]
        )

        hits = keyword_search(corpus, ["partial auc"], 5)

        self.assertEqual(["p1:c0"], [hit.chunk_id for hit in hits])

    async def test_a_single_word_query_with_no_match_stays_empty(self) -> None:
        """单词查询拆不出更多词，没有可退的一步，空就是空。"""
        corpus = await self.load(["Nothing relevant here."])

        self.assertEqual([], keyword_search(corpus, ["wasserstein"], 5))


class ReferenceEdgeTest(unittest.TestCase):
    """论文级引用边：靠解析参考文献连不出来的那些，由上游 references 补。

    真机实测解析参考文献在 20 篇语料上只连出 1 条边、44 篇 16 条——在生产尺寸上
    ``paper_cites`` 与 ``cited_by`` 等于是死的。
    """

    @staticmethod
    def _units(namespace: str, count: int = 2) -> list:
        from athena.research.literature.paper_markdown.models import RetrievalUnit

        return [
            RetrievalUnit(
                unit_id=f"{namespace}:chunk-{index}",
                kind="abstract" if index == 0 else "paragraph",
                text=f"Body {index} of {namespace} with enough prose to be an anchor.",
                heading_path=[],
                locators=[],
                metadata={"retrieval_namespace": namespace, "paper_id": namespace},
            )
            for index in range(count)
        ]

    def test_edges_land_on_the_anchor_of_each_paper(self) -> None:
        """反向查询本来就把任意 chunk 归约到锚点；正向用同一个落点，两个方向才对称。"""
        units = [self._units("p1"), self._units("p2")]
        anchors = paper_anchors(units)

        edges = reference_edges(units, anchors, {"p1": ["p2"]})

        self.assertEqual({anchors["p1"]: [anchors["p2"]]}, edges)

    def test_targets_outside_the_corpus_are_dropped(self) -> None:
        """跨出语料的引用没有落点，留着只会让 Agent 读到 not_found。"""
        units = [self._units("p1")]
        anchors = paper_anchors(units)

        edges = reference_edges(units, anchors, {"p1": ["p2", "p3"]})

        self.assertEqual({}, edges)

    def test_a_paper_never_cites_itself(self) -> None:
        units = [self._units("p1")]
        anchors = paper_anchors(units)

        self.assertEqual({}, reference_edges(units, anchors, {"p1": ["p1"]}))

    def test_duplicate_targets_are_collapsed(self) -> None:
        units = [self._units("p1"), self._units("p2")]
        anchors = paper_anchors(units)

        edges = reference_edges(units, anchors, {"p1": ["p2", "p2"]})

        self.assertEqual([anchors["p2"]], edges[anchors["p1"]])

    def test_an_unknown_source_is_skipped_rather_than_raising(self) -> None:
        units = [self._units("p1")]
        anchors = paper_anchors(units)

        self.assertEqual({}, reference_edges(units, anchors, {"ghost": ["p1"]}))

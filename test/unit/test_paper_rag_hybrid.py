"""Phase 3 的三处改动：门面锚点、融合检索、查询向量缓存与 mmap 装载。

这三件事的共同点是**失败时都不报错**——锚点落在表格上、融合退化成单通道、向量装载多占
几百 MB，链路照跑不误。所以它们各自需要一条断言，而不是靠"跑通了"来判断。
"""

import asyncio
import tempfile
import unittest

import numpy

from athena.core.artifact_store import LocalArtifactStore
from athena.research.literature.paper_rag.index import (
    MIN_ANCHOR_PROSE_CHARS,
    anchor_index,
    novel_prose_chars,
    pack_vectors,
)
from athena.research.literature.paper_rag.models import (
    CorpusEntry,
    CorpusSentence,
    PaperCorpusIndex,
    SearchHit,
)
from athena.research.literature.paper_rag.search import (
    CorpusCache,
    RetrievalSession,
    _mapped_vectors,
    hybrid_search,
    reciprocal_rank_fusion,
)

PROSE = (
    "We reweight the minority class and report consistent gains on nine tabular "
    "benchmarks under severe imbalance."
)


def _hit(chunk_id: str, score: float = 1.0, snippet: str = "") -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        paper_id=chunk_id.split(":")[0],
        kind="paragraph",
        score=score,
        snippet=snippet,
    )


class AnchorTest(unittest.TestCase):
    """门面锚点同时是总览摘要、引用边落点和写进提示词的语料目录。"""

    def test_an_abstract_wins_wherever_it_sits(self) -> None:
        kinds = ["paragraph", "table", "abstract"]

        self.assertEqual(2, anchor_index(kinds, [PROSE, "| a |", PROSE], ["", "", ""]))

    def test_a_table_first_chunk_is_skipped_for_the_prose_behind_it(self) -> None:
        """真机上 3 篇的门面落在表格、1 篇落在插图——``paper_cites`` 指过去就是一张表。"""
        kinds = ["table", "paragraph"]

        self.assertEqual(1, anchor_index(kinds, ["| a | b |", PROSE], ["", ""]))

    def test_front_matter_that_only_repeats_the_title_is_skipped(self) -> None:
        """标题已经在 ``title`` 字段里另给了一份，重复一遍不是新信息。"""
        title = "Imbalance XGBoost leveraging weighted and focal losses"

        self.assertEqual(
            1, anchor_index(["paragraph", "paragraph"], [title, PROSE], [title, title])
        )

    def test_a_latex_preamble_is_not_a_front_door(self) -> None:
        preamble = r"\definecolor{mydarkblue}{rgb}{0,0.08,0.45} \hypersetup{pdftitle=}"

        self.assertEqual(
            1, anchor_index(["paragraph", "paragraph"], [preamble, PROSE], ["", ""])
        )

    def test_the_scan_stops_before_wandering_into_the_body(self) -> None:
        """前置区块就那么长；再往后就成了"随便找一段正文"，那不是门面。"""
        kinds = ["table"] * 10 + ["paragraph"]
        texts = ["| a |"] * 10 + [PROSE]

        self.assertEqual(0, anchor_index(kinds, texts, [""] * 11))

    def test_a_paper_with_nothing_usable_still_gets_an_anchor(self) -> None:
        self.assertEqual(0, anchor_index(["table"], ["| a |"], [""]))

    def test_prose_measurement_ignores_the_section_prefix(self) -> None:
        prefixed = f"> Section: Introduction\n\n{PROSE}"

        self.assertEqual(novel_prose_chars(PROSE, ""), novel_prose_chars(prefixed, ""))
        self.assertGreater(novel_prose_chars(PROSE, ""), MIN_ANCHOR_PROSE_CHARS)


class FusionTest(unittest.TestCase):
    def test_fusion_uses_ranks_because_the_two_scores_are_not_comparable(self) -> None:
        """词面分无上界，语义分在 0..1；任何线性相加都要先猜一个归一化。"""
        lexical = [_hit("p1:a", score=9000.0), _hit("p2:a", score=10.0)]
        semantic = [_hit("p2:a", score=0.81), _hit("p1:a", score=0.80)]

        fused = reciprocal_rank_fusion([lexical, semantic], 2)

        # 两者名次互为镜像，因此得分相同，次序退化为 chunk_id 的确定性排序
        self.assertEqual(fused[0].score, fused[1].score)
        self.assertEqual(["p1:a", "p2:a"], [item.chunk_id for item in fused])

    def test_a_chunk_both_channels_return_outranks_one_either_returns_alone(
        self,
    ) -> None:
        """两条独立证据都指向它——这正是融合想要的。"""
        lexical = [_hit("solo:a"), _hit("both:a")]
        semantic = [_hit("other:a"), _hit("both:a")]

        fused = reciprocal_rank_fusion([lexical, semantic], 3)

        self.assertEqual("both:a", fused[0].chunk_id)

    def test_the_snippet_comes_from_the_better_ranked_channel(self) -> None:
        lexical = [_hit("x:a", snippet="from keywords")]
        semantic = [_hit("y:a"), _hit("x:a", snippet="from meaning")]

        fused = reciprocal_rank_fusion([lexical, semantic], 2)

        self.assertEqual("from keywords", fused[0].snippet)

    def test_an_empty_channel_contributes_nothing_and_breaks_nothing(self) -> None:
        fused = reciprocal_rank_fusion([[], [_hit("x:a")]], 5)

        self.assertEqual(["x:a"], [item.chunk_id for item in fused])


class HybridSearchTest(unittest.TestCase):
    def _corpus(self):
        entries = [
            CorpusEntry(
                chunk_id="p1:a",
                paper_id="p1",
                kind="paragraph",
                text="focal loss for boosted trees on imbalanced tables",
                sentence_start=0,
                sentence_end=1,
            ),
            CorpusEntry(
                chunk_id="p2:a",
                paper_id="p2",
                kind="paragraph",
                text="protein folding kinetics in aqueous solution",
                sentence_start=1,
                sentence_end=2,
            ),
        ]
        sentences = [
            CorpusSentence(entry_index=0, char_start=0, char_end=len(entries[0].text)),
            CorpusSentence(entry_index=1, char_start=0, char_end=len(entries[1].text)),
        ]
        from athena.research.literature.paper_rag.search import LoadedCorpus

        return LoadedCorpus(
            index=PaperCorpusIndex(entries=entries, sentences=sentences),
            positions={"p1:a": 0, "p2:a": 1},
            cited_by={},
            lowered=[item.text.lower() for item in entries],
        )

    def test_without_vectors_it_degrades_to_the_lexical_half(self) -> None:
        """整个入口变成硬错误只会逼 Agent 去猜该换哪个工具。"""
        hits = hybrid_search(self._corpus(), [], ["focal"], 5)

        self.assertEqual(["p1:a"], [item.chunk_id for item in hits])

    def test_without_keywords_it_degrades_to_the_semantic_half(self) -> None:
        corpus = self._corpus()
        corpus.vectors = numpy.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=numpy.float32)
        corpus.weights = numpy.asarray([1.0, 1.0], dtype=numpy.float32)

        hits = hybrid_search(corpus, [1.0, 0.0], [], 5)

        self.assertEqual(["p1:a"], [item.chunk_id for item in hits])

    def test_with_neither_it_returns_nothing_rather_than_raising(self) -> None:
        self.assertEqual([], hybrid_search(self._corpus(), [], [], 5))


class CountingEmbedder:
    model = "fake-embedder"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


class QueryVectorCacheTest(unittest.IsolatedAsyncioTestCase):
    """语义检索 96% 的时间是一次编码 API 往返（p50 136.8ms，本地打分只要 6.66ms）。"""

    async def test_the_same_question_is_encoded_once_across_sessions(self) -> None:
        """三条 Ideator lane 拿到的是同一个任务、同一份语料，问出来的话高度重合。"""
        cache = CorpusCache()
        embedder = CountingEmbedder()
        first, second = RetrievalSession(cache), RetrievalSession(cache)

        await first.embed_query(embedder, "class imbalance and AUC")
        await second.embed_query(embedder, "class imbalance and AUC")

        self.assertEqual(1, embedder.calls)

    async def test_a_different_question_is_encoded_again(self) -> None:
        cache = CorpusCache()
        embedder = CountingEmbedder()

        await cache.embed_query(embedder, "one")
        await cache.embed_query(embedder, "two")

        self.assertEqual(2, embedder.calls)

    async def test_two_embedders_never_share_a_cached_vector(self) -> None:
        """不同模型的向量不在同一个空间里，混用会给出看上去正常的无意义分数。"""
        cache = CorpusCache()
        left, right = CountingEmbedder(), CountingEmbedder()
        right.model = "other-embedder"

        await cache.embed_query(left, "same question")
        await cache.embed_query(right, "same question")

        self.assertEqual(1, left.calls)
        self.assertEqual(1, right.calls)

    async def test_the_cached_vector_is_already_normalised(self) -> None:
        class Unnormalised(CountingEmbedder):
            async def embed(self, texts):
                self.calls += 1
                return [[3.0, 4.0] for _ in texts]

        vector = await CorpusCache().embed_query(Unnormalised(), "q")

        self.assertAlmostEqual(1.0, sum(value * value for value in vector) ** 0.5)


class MappedVectorsTest(unittest.TestCase):
    """读字节要同时持有字节缓冲与数组；实测 44 篇语料峰值 519 MB vs mmap 367 MB。"""

    def test_a_local_store_is_opened_as_a_memmap(self) -> None:
        store = LocalArtifactStore(tempfile.mkdtemp(prefix="mmap_"))
        block = numpy.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=numpy.float32)
        ref = asyncio.run(store.put_bytes(pack_vectors(block)))

        mapped = _mapped_vectors(store, ref)

        self.assertIsInstance(mapped, numpy.memmap)
        numpy.testing.assert_allclose(block, numpy.asarray(mapped))

    def test_a_store_without_paths_falls_back_instead_of_failing(self) -> None:
        """协议里没有 ``path_for``；换成远端实现时这里必须自动退回读字节。"""

        class Remote:
            pass

        self.assertIsNone(_mapped_vectors(Remote(), "sha256:" + "0" * 64))

    def test_a_missing_file_falls_back_rather_than_raising(self) -> None:
        store = LocalArtifactStore(tempfile.mkdtemp(prefix="mmap_"))

        self.assertIsNone(_mapped_vectors(store, "sha256:" + "0" * 64))


if __name__ == "__main__":
    unittest.main()

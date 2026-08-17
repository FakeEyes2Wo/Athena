"""基准本身的回归用例：先证明尺子是准的，再拿它去量别的东西。

这些用例全部跑在**构造语料**上，因为它们验的是计分逻辑而不是检索质量：一把尺子必须在
已知答案上给出已知读数，否则它报出来的任何改进都无从判断真假。真实语料上的数字由
``Athena-cli bench`` 产生，不进 CI——那需要网络与编码器。
"""

import unittest

from pydantic import ValidationError

from athena.research.bench.health import (
    MIN_ANCHOR_PROSE_CHARS,
    all_headings,
    corpus_health,
    novel_prose_chars,
)
from athena.research.bench.known_item import (
    KEYWORD_CHANNEL,
    SEMANTIC_CHANNEL,
    compare,
    distinct_papers,
    first_gold_rank,
    run_channel,
    run_known_item,
    score_channel,
    usable_queries,
)
from athena.research.bench.query_sets import (
    available,
    load_query_set,
    load_recall_set,
)
from athena.research.bench.recall import RecallQuerySet, evaluate_recall
from athena.research.bench.reproducibility import delivery_overlap, jaccard
from athena.research.bench.schemas import KnownItemQuery, QueryOutcome, QuerySet
from athena.research.paper_rag.schemas import (
    CorpusEntry,
    CorpusSentence,
    PaperCorpusIndex,
    SearchHit,
)
from athena.research.paper_rag.search import LoadedCorpus, keyword_search


def _entry(
    chunk_id: str,
    paper_id: str,
    text: str,
    *,
    kind: str = "paragraph",
    title: str = "",
    heading_path: list[str] | None = None,
    cited_ids: list[str] | None = None,
    visual_ids: list[str] | None = None,
    sentence_start: int = 0,
    sentence_end: int = 1,
) -> CorpusEntry:
    return CorpusEntry(
        chunk_id=chunk_id,
        paper_id=paper_id,
        title=title,
        kind=kind,
        heading_path=heading_path or [],
        text=text,
        cited_ids=cited_ids or [],
        visual_ids=visual_ids or [],
        sentence_start=sentence_start,
        sentence_end=sentence_end,
    )


def _corpus(entries: list[CorpusEntry], **index_fields) -> LoadedCorpus:
    """把若干条目组装成一份可检索的语料，句子按条目正文整段登记。"""
    sentences: list[CorpusSentence] = []
    for position, entry in enumerate(entries):
        entry.sentence_start = len(sentences)
        sentences.append(
            CorpusSentence(entry_index=position, char_start=0, char_end=len(entry.text))
        )
        entry.sentence_end = len(sentences)
    index = PaperCorpusIndex(entries=entries, sentences=sentences, **index_fields)
    cited_by: dict[str, list[str]] = {}
    for entry in entries:
        for target in entry.cited_ids:
            cited_by.setdefault(target, []).append(entry.chunk_id)
    return LoadedCorpus(
        index=index,
        positions={entry.chunk_id: i for i, entry in enumerate(entries)},
        cited_by=cited_by,
        lowered=[entry.text.lower() for entry in entries],
    )


def _hit(chunk_id: str, paper_id: str) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id, paper_id=paper_id, kind="paragraph", score=1.0, snippet=""
    )


class RankingTest(unittest.TestCase):
    """名次按论文算，不按 chunk 算。"""

    def test_one_paper_filling_the_page_still_counts_as_one_rank(self) -> None:
        """同一篇的三个 chunk 不该让它占据第 1、2、3 名。

        按 chunk 算名次会让"名额被一篇吃光"这种失败反而抬高分数——那正是关键词检索
        真机上最严重的失效，尺子必须能看见它而不是奖励它。
        """
        hits = [_hit("p1:a", "p1"), _hit("p1:b", "p1"), _hit("p2:a", "p2")]

        self.assertEqual(["p1", "p2"], distinct_papers(hits))
        self.assertEqual(2, first_gold_rank(distinct_papers(hits), ["p2"]))

    def test_any_gold_counts_and_the_earliest_one_wins(self) -> None:
        returned = ["p3", "p1", "p2"]

        self.assertEqual(2, first_gold_rank(returned, ["p1", "p2"]))

    def test_nothing_returned_is_a_miss_not_a_zero_rank(self) -> None:
        """未命中必须是 ``None``，因为 0 会被 ``1/rank`` 变成除零或无穷大。"""
        self.assertIsNone(first_gold_rank(["p9"], ["p1"]))


class ChannelScoreTest(unittest.TestCase):
    def test_mrr_and_cutoffs_match_the_hand_computed_values(self) -> None:
        outcomes = [
            QueryOutcome(query_id="a", rank=1),
            QueryOutcome(query_id="b", rank=4),
            QueryOutcome(query_id="c", rank=None),
        ]

        score = score_channel("k", outcomes)

        self.assertEqual(3, score.scored)
        self.assertEqual(1, score.hit_at_1)
        self.assertEqual(1, score.hit_at_3)
        self.assertEqual(2, score.hit_at_5)
        self.assertEqual(1, score.missed)
        self.assertAlmostEqual((1.0 + 0.25) / 3, score.mrr)

    def test_an_empty_channel_scores_zero_instead_of_dividing_by_zero(self) -> None:
        self.assertEqual(0.0, score_channel("k", []).mrr)


class GoldPresenceTest(unittest.TestCase):
    """金标不在语料里是出题问题，不是检索失败。"""

    def test_a_gold_missing_from_the_corpus_is_excluded_not_counted_as_a_miss(
        self,
    ) -> None:
        """真机上这一步当场抓出过一个错标；没有它，出题错误会伪装成检索失败。"""
        corpus = _corpus([_entry("p1:a", "p1", "anything")])
        query_set = QuerySet(
            name="t",
            queries=[
                KnownItemQuery(query_id="here", question="q", gold=["p1"]),
                KnownItemQuery(query_id="absent", question="q", gold=["p404"]),
            ],
        )

        usable, unusable = usable_queries(query_set, corpus)

        self.assertEqual(["here"], usable)
        self.assertEqual(["absent"], unusable)


class KnownItemRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_keyword_channel_finds_the_paper_that_carries_the_term(
        self,
    ) -> None:
        corpus = _corpus(
            [
                _entry("p1:a", "p1", "This paper studies focal loss for boosted trees."),
                _entry("p2:a", "p2", "This paper studies protein folding kinetics."),
            ]
        )
        query_set = QuerySet(
            name="t",
            queries=[
                KnownItemQuery(
                    query_id="k1",
                    question="up-weight hard examples in a tree ensemble",
                    keywords=["focal"],
                    gold=["p1"],
                )
            ],
        )

        report = await run_known_item(corpus, query_set)

        self.assertEqual([KEYWORD_CHANNEL], [item.channel for item in report.channels])
        self.assertEqual(1, report.channels[0].hit_at_1)
        self.assertEqual(1.0, report.channels[0].mrr)

    async def test_the_semantic_channel_is_skipped_when_the_corpus_has_no_vectors(
        self,
    ) -> None:
        """没有向量时只跑词面通道而不是报错——关键词检索的回归照样值得盯。"""

        class Embedder:
            model = "fake"

            async def embed(self, texts: list[str]) -> list[list[float]]:
                return [[1.0, 0.0] for _ in texts]

        corpus = _corpus([_entry("p1:a", "p1", "focal loss")])
        query_set = QuerySet(
            name="t",
            queries=[KnownItemQuery(query_id="k1", question="q", gold=["p1"])],
        )

        report = await run_known_item(corpus, query_set, embedder=Embedder())

        self.assertNotIn(SEMANTIC_CHANNEL, [item.channel for item in report.channels])

    async def test_a_channel_that_raises_records_the_error_and_keeps_going(self) -> None:
        """一条查询炸掉不该丢掉整个通道的成绩，否则一次偶发失败就抹掉全部读数。"""
        corpus = _corpus([_entry("p1:a", "p1", "focal loss")])
        query_set = QuerySet(
            name="t",
            queries=[
                KnownItemQuery(query_id="bad", question="boom", gold=["p1"]),
                KnownItemQuery(
                    query_id="ok", question="q", keywords=["focal"], gold=["p1"]
                ),
            ],
        )

        async def flaky(question: str, keywords: list[str], top_k: int):
            if question == "boom":
                raise RuntimeError("upstream said no")
            return keyword_search(corpus, keywords, top_k)

        score = await run_channel(query_set, ["bad", "ok"], KEYWORD_CHANNEL, flaky, 10)

        self.assertEqual(2, score.scored)
        by_id = {item.query_id: item for item in score.outcomes}
        self.assertIn("upstream said no", by_id["bad"].error)
        self.assertIsNone(by_id["bad"].rank)
        self.assertEqual(1, by_id["ok"].rank)


class CompareTest(unittest.TestCase):
    def test_a_swap_that_leaves_mrr_flat_still_shows_up_per_query(self) -> None:
        """汇总指标会把互相抵消的变化藏起来，逐条比对不会。"""
        before = await_free_report([("a", 1), ("b", 3)])
        after = await_free_report([("a", 3), ("b", 1)])

        lines = compare(before, after)

        self.assertTrue(any("a: 1 -> 3" in line for line in lines))
        self.assertTrue(any("b: 3 -> 1" in line for line in lines))


def await_free_report(pairs: list[tuple[str, int | None]]):
    """构造一份只有名次信息的报告，用于比对逻辑的用例。"""
    from athena.research.bench.schemas import RetrievalBenchReport

    return RetrievalBenchReport(
        query_set="t",
        corpus_ref="",
        corpus_papers=0,
        corpus_chunks=0,
        channels=[
            score_channel(
                KEYWORD_CHANNEL,
                [QueryOutcome(query_id=name, rank=rank) for name, rank in pairs],
            )
        ],
    )


class CorpusHealthTest(unittest.TestCase):
    def test_a_paper_without_an_abstract_chunk_is_reported_with_its_fallback_anchor(
        self,
    ) -> None:
        """锚点回退成表格或插图时，Ideator 看到的"摘要"就是一张表。"""
        corpus = _corpus(
            [
                _entry("p1:a", "p1", "| a | b |", kind="table", title="T"),
                _entry("p1:b", "p1", "Body text here.", title="T"),
                _entry("p2:a", "p2", "We propose a method.", kind="abstract", title="U"),
            ]
        )

        report = corpus_health(corpus, "sha256:x")

        by_id = {item.paper_id: item for item in report.papers_detail}
        self.assertFalse(by_id["p1"].has_abstract_chunk)
        self.assertEqual("table", by_id["p1"].anchor_kind)
        self.assertTrue(by_id["p2"].has_abstract_chunk)
        self.assertEqual(0.5, report.abstract_coverage)

    def test_citation_edges_are_counted_per_paper_pair_not_per_chunk(self) -> None:
        """一篇论文在三个 chunk 里引同一篇，仍然只是一条边。

        按 chunk 计会让密度看上去比实际好，而这个数字的用途正是判断
        ``paper_cites`` 走不走得动。
        """
        corpus = _corpus(
            [
                _entry("p1:a", "p1", "x", cited_ids=["p2:a"]),
                _entry("p1:b", "p1", "y", cited_ids=["p2:a"]),
                _entry("p1:c", "p1", "z", cited_ids=["p2:a"]),
                _entry("p2:a", "p2", "w"),
            ]
        )

        report = corpus_health(corpus)

        self.assertEqual(1, report.citation_edges)
        self.assertEqual(0.5, report.citation_density)

    def test_section_coverage_looks_at_every_heading_level(self) -> None:
        """Ablation 几乎总是子节；只看一级标题会把它报成 0，而 section_search 找得到。"""
        corpus = _corpus(
            [
                _entry(
                    "p1:a",
                    "p1",
                    "removing the reranker costs the gain",
                    heading_path=["Experiments", "Ablation Study"],
                )
            ]
        )

        report = corpus_health(corpus)

        self.assertEqual(1, report.section_coverage["ablation"])
        self.assertEqual(1, report.section_coverage["experiment"])

    def test_section_aliases_are_honoured_so_wording_does_not_decide_coverage(
        self,
    ) -> None:
        corpus = _corpus(
            [_entry("p1:a", "p1", "x", heading_path=["Threats to Validity"])]
        )

        self.assertEqual(1, corpus_health(corpus).section_coverage["limitation"])

    def test_all_headings_keeps_document_order_and_deduplicates(self) -> None:
        corpus = _corpus(
            [
                _entry("p1:a", "p1", "x", heading_path=["Method", "Loss"]),
                _entry("p1:b", "p1", "y", heading_path=["Method", "Loss"]),
            ]
        )

        self.assertEqual(["Method", "Loss"], all_headings(corpus, [0, 1]))


class AnchorProseTest(unittest.TestCase):
    """门面额度买到了多少新信息。"""

    def test_a_latex_preamble_anchor_counts_as_empty(self) -> None:
        """真机上有一篇的锚点整段是 ``\\definecolor…pdftitle=``。"""
        text = r"\definecolor{mydarkblue}{rgb}{0,0.08,0.45} \hypersetup{pdftitle=}"

        self.assertLess(novel_prose_chars(text, ""), MIN_ANCHOR_PROSE_CHARS)

    def test_repeating_the_title_buys_nothing(self) -> None:
        """标题已经在 ``PaperSummary.title`` 里另给了一份，重复一遍不是新信息。"""
        title = "Imbalance XGBoost leveraging weighted and focal losses"

        self.assertLess(novel_prose_chars(title, title), MIN_ANCHOR_PROSE_CHARS)

    def test_real_abstract_prose_is_counted(self) -> None:
        text = (
            "We show that reweighting the minority class changes the decision "
            "threshold and report consistent gains across nine tabular datasets."
        )

        self.assertGreater(novel_prose_chars(text, "Some Title"), MIN_ANCHOR_PROSE_CHARS)

    def test_the_structural_section_prefix_is_stripped_before_counting(self) -> None:
        body = "We reweight the minority class and report gains on nine datasets here."
        prefixed = f"> Section: Introduction\n\n{body}"

        self.assertEqual(novel_prose_chars(body, ""), novel_prose_chars(prefixed, ""))


class DeliveryOverlapTest(unittest.TestCase):
    def test_two_identical_runs_are_perfectly_reproducible(self) -> None:
        report = delivery_overlap([["a", "b"], ["b", "a"]])

        self.assertEqual(1.0, report.mean_jaccard)
        self.assertEqual(["a", "b"], report.stable_core)

    def test_the_measured_shape_of_the_real_defect(self) -> None:
        """真机形态：同一查询两次交付 10 篇，只有 2 篇相同。"""
        first = [f"p{i}" for i in range(10)]
        second = ["p0", "p1"] + [f"q{i}" for i in range(8)]

        report = delivery_overlap(first_and(first, second), query="imbalance")

        self.assertAlmostEqual(2 / 18, report.mean_jaccard)
        self.assertEqual(["p0", "p1"], report.stable_core)
        self.assertEqual(18, report.union_size)

    def test_comparing_a_single_run_is_rejected_rather_than_reported_as_perfect(
        self,
    ) -> None:
        """一次跑测没有可复现性可言；返回 1.0 会是最危险的那种错误答案。"""
        with self.assertRaises(ValueError):
            delivery_overlap([["a"]])

    def test_two_empty_runs_agree_rather_than_disagree(self) -> None:
        self.assertEqual(1.0, jaccard(set(), set()))


def first_and(*runs: list[str]) -> list[list[str]]:
    """可读性辅助：把若干次交付列表拼成 ``delivery_overlap`` 的入参。"""
    return list(runs)


class PackagedQuerySetTest(unittest.TestCase):
    def test_the_packaged_set_loads_and_every_query_carries_a_rationale(self) -> None:
        """金标改动必须能在 diff 里看见理由——不然前后两次基准的不可比是静默的。"""
        query_set = load_query_set()

        self.assertIn("imbalance_auc", available())
        self.assertEqual(12, len(query_set.queries))
        for query in query_set.queries:
            self.assertTrue(query.rationale, query.query_id)
            self.assertTrue(query.gold, query.query_id)

    def test_query_ids_are_unique_so_two_runs_can_be_diffed(self) -> None:
        ids = [item.query_id for item in load_query_set().queries]

        self.assertEqual(len(ids), len(set(ids)))

    def test_an_unknown_name_names_the_sets_that_do_exist(self) -> None:
        with self.assertRaises(FileNotFoundError) as caught:
            load_query_set("nope")

        self.assertIn("imbalance_auc", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class RecallTest(unittest.TestCase):
    """三段损失必须分开报——它们该动的地方完全不同。"""

    @staticmethod
    def _set(gold: list[str]) -> RecallQuerySet:
        return RecallQuerySet(
            name="t", topic="a topic", gold=gold, gold_source="a stronger run"
        )

    def test_a_gold_never_retrieved_is_charged_to_retrieval(self) -> None:
        report = evaluate_recall(self._set(["a", "b"]), {"a": 1.0}, ["a"])

        stages = {item.stage: item for item in report.stages}
        self.assertEqual(1, stages["in_pool"].lost_here)
        self.assertEqual(["b"], report.missing_from_pool)

    def test_a_gold_found_but_scored_low_is_charged_to_scoring(self) -> None:
        """找到了却没判够分——该动的是打分提示或打分模型，不是检索。"""
        report = evaluate_recall(self._set(["a", "b"]), {"a": 1.0, "b": 0.2}, ["a"])

        stages = {item.stage: item for item in report.stages}
        self.assertEqual(0, stages["in_pool"].lost_here)
        self.assertEqual(1, stages["judged_relevant"].lost_here)
        self.assertEqual(["b"], report.scored_below_threshold)

    def test_a_gold_judged_relevant_but_cut_is_charged_to_delivery(self) -> None:
        report = evaluate_recall(self._set(["a", "b"]), {"a": 1.0, "b": 1.0}, ["a"])

        stages = {item.stage: item for item in report.stages}
        self.assertEqual(1, stages["delivered"].lost_here)
        self.assertEqual(["b"], report.dropped_at_delivery)

    def test_every_stage_is_scored_against_the_gold_total(self) -> None:
        """分母都是金标总数：读数的人问的是"最终拿到多少"，各段自己丢多少由 lost_here 答。"""
        report = evaluate_recall(
            self._set(["a", "b", "c", "d"]), {"a": 1.0, "b": 1.0, "c": 0.2}, ["a"]
        )

        stages = {item.stage: item for item in report.stages}
        self.assertAlmostEqual(0.75, stages["in_pool"].recall)
        self.assertAlmostEqual(0.5, stages["judged_relevant"].recall)
        self.assertAlmostEqual(0.25, stages["delivered"].recall)

    def test_the_threshold_is_inclusive_at_the_mid_tier(self) -> None:
        """0.45 是 2 分档的取值——"切题但不满足全部条件"应当算认。"""
        report = evaluate_recall(self._set(["a"]), {"a": 0.45}, ["a"])

        self.assertEqual([], report.scored_below_threshold)

    def test_a_duplicated_gold_is_counted_once(self) -> None:
        report = evaluate_recall(self._set(["a", "a"]), {"a": 1.0}, ["a"])

        self.assertEqual(1, report.gold_total)

    def test_the_gold_source_is_carried_into_the_report(self) -> None:
        """代理金标被当成 ground truth 读，比没有金标更糟。"""
        report = evaluate_recall(self._set(["a"]), {"a": 1.0}, ["a"])

        self.assertEqual("a stronger run", report.gold_source)

    def test_a_gold_source_cannot_be_left_empty(self) -> None:
        with self.assertRaises(ValidationError):
            RecallQuerySet(name="t", topic="x", gold=["a"], gold_source="")


class PackagedRecallSetTest(unittest.TestCase):
    def test_the_packaged_recall_gold_loads_and_declares_its_source(self) -> None:
        recall_set = load_recall_set("imbalance_auc_recall")

        self.assertTrue(recall_set.gold)
        self.assertIn("PROXY", recall_set.gold_source)

"""选片的回归用例：交付集合不能再由散列决定。

这些用例围着一个事实转——打分只有四档，交付名额几乎**总是**落在某一档内部。真机实测
一轮里 8 篇满分直接入选，剩下 5 个名额由 35 篇同为 0.45 的论文争夺。此前决定这 5 篇的
是 ``tie_break`` 的 sha256，后果是同一查询两次跑测只有 2/10 重合。

因此这里钉住三件事：确定入选的那几篇不受重排影响、边界档按分面覆盖填、以及**任何一步
出问题都必须仍然给得出一份交付集合**。
"""

import unittest

from athena.research.bench.reproducibility import delivery_overlap
from athena.research.paper_scout.pool import tie_break
from athena.research.paper_scout.schemas import ScoutPaper
from athena.research.paper_scout.selection import (
    DeliverySelection,
    fill_by_coverage,
    format_papers,
    hash_order,
    parse_selection,
    select_delivery,
    split_at_cut,
)


def _paper(key: str, score: float, title: str = "") -> ScoutPaper:
    return ScoutPaper(
        paper_key=key,
        title=title or f"Paper {key}",
        abstract=f"Abstract of {key}",
        source="search",
        relevance=score,
    )


def _ranked(scores: dict[str, float]) -> list[ScoutPaper]:
    """按 ``PaperPool.ranked()`` 的口径排序：分数降序，同分按散列。"""
    papers = [_paper(key, score) for key, score in scores.items()]
    return sorted(papers, key=lambda item: (-item.relevance, tie_break(item.paper_key)))


class FakeSelector:
    """按给定次序与分面标注回答的重排器。"""

    def __init__(self, order: list[str], facets: list[str], tags: dict[str, list[str]]):
        self.order = order
        self.facets = facets
        self.tags = tags
        self.calls = 0
        self.seen: tuple[list[str], list[str]] | None = None

    async def rank(self, query, settled, tied):
        self.calls += 1
        self.seen = (
            [item.paper_key for item in settled],
            [item.paper_key for item in tied],
        )
        return self.facets, self.tags, self.order


class ExplodingSelector:
    def __init__(self) -> None:
        self.calls = 0

    async def rank(self, query, settled, tied):
        self.calls += 1
        raise RuntimeError("upstream refused")


class SplitAtCutTest(unittest.TestCase):
    def test_the_tier_that_straddles_the_cut_is_the_one_to_resolve(self) -> None:
        """打分四档，名额落在档内部——这就是要重排的那一档。"""
        papers = _ranked({"a": 1.0, "b": 1.0, **{f"t{i}": 0.45 for i in range(5)}})

        certain, tied = split_at_cut(papers, 4)

        self.assertEqual({"a", "b"}, {item.paper_key for item in certain})
        self.assertEqual(5, len(tied))

    def test_no_cut_when_everything_fits(self) -> None:
        papers = _ranked({"a": 1.0, "b": 0.45})

        certain, tied = split_at_cut(papers, 10)

        self.assertEqual(2, len(certain))
        self.assertEqual([], tied)

    def test_a_zero_limit_means_no_cap_not_no_papers(self) -> None:
        """``ScoutRequest.max_papers`` 默认是 0，含义是"不截断"。"""
        papers = _ranked({"a": 1.0, "b": 0.45})

        certain, tied = split_at_cut(papers, 0)

        self.assertEqual(2, len(certain))
        self.assertEqual([], tied)


class SelectDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_cap_delivers_everything(self) -> None:
        papers = _ranked({"a": 1.0, "b": 0.45})

        selection = await select_delivery("q", papers, 0, FakeSelector([], [], {}))

        self.assertEqual(2, len(selection.delivered))

    async def test_papers_above_the_cut_are_never_reranked(self) -> None:
        """它们的分数本来就更高；重排只会用一个更弱的信号去覆盖一个更强的。"""
        papers = _ranked({"a": 1.0, "b": 1.0, "t1": 0.45, "t2": 0.45, "t3": 0.45})
        selector = FakeSelector(["t3", "t2", "t1"], ["f"], {"t3": ["f"]})

        selection = await select_delivery("q", papers, 3, selector)

        self.assertEqual(["a", "b"], sorted(item.paper_key for item in selection.delivered[:2]))
        self.assertEqual(["a", "b"], sorted(selector.seen[0]))
        self.assertEqual({"t1", "t2", "t3"}, set(selector.seen[1]))

    async def test_the_boundary_order_decides_the_last_slots(self) -> None:
        papers = _ranked({"a": 1.0, "t1": 0.45, "t2": 0.45, "t3": 0.45})
        selector = FakeSelector(["t3", "t1", "t2"], [], {})

        selection = await select_delivery("q", papers, 3, selector)

        self.assertEqual(["a", "t3", "t1"], [item.paper_key for item in selection.delivered])
        self.assertTrue(selection.reranked)
        self.assertEqual(3, selection.boundary_size)

    async def test_an_uncovered_facet_outranks_a_better_paper_on_a_covered_one(
        self,
    ) -> None:
        """"再多一篇讲同一个方法的"远不如"第一篇讲主指标的"。

        按相关性排序无法表达这件事——同一档里的论文按定义分数相同。
        """
        papers = _ranked({"t1": 0.45, "t2": 0.45, "t3": 0.45})
        selector = FakeSelector(
            ["t1", "t2", "t3"],
            ["oversampling", "auc"],
            {"t1": ["oversampling"], "t2": ["oversampling"], "t3": ["auc"]},
        )

        selection = await select_delivery("q", papers, 2, selector)

        delivered = [item.paper_key for item in selection.delivered]
        self.assertIn("t3", delivered)
        self.assertEqual(1.0, selection.coverage())

    async def test_facets_already_covered_by_settled_papers_are_not_chased_again(
        self,
    ) -> None:
        papers = _ranked({"a": 1.0, "t1": 0.45, "t2": 0.45, "t3": 0.45})
        selector = FakeSelector(
            ["t1", "t2", "t3"],
            ["auc", "trees"],
            {"a": ["auc"], "t1": ["trees"], "t2": ["auc"], "t3": ["auc"]},
        )

        selection = await select_delivery("q", papers, 2, selector)

        self.assertEqual(["a", "t1"], [item.paper_key for item in selection.delivered])

    async def test_a_paper_the_model_forgot_lands_at_the_back_not_out(self) -> None:
        """漏掉一篇不该让它彻底出局，但也不该插到前面。"""
        papers = _ranked({"t1": 0.45, "t2": 0.45, "t3": 0.45})
        selector = FakeSelector(["t2"], [], {})

        selection = await select_delivery("q", papers, 2, selector)

        self.assertEqual("t2", selection.delivered[0].paper_key)
        self.assertEqual(2, len(selection.delivered))

    async def test_no_call_is_made_when_the_boundary_tier_already_fits(self) -> None:
        """重排改变不了任何结果时，那一次调用就是白花的。"""
        papers = _ranked({"a": 1.0, "t1": 0.45, "t2": 0.45})
        selector = FakeSelector([], [], {})

        selection = await select_delivery("q", papers, 3, selector)

        self.assertEqual(0, selector.calls)
        self.assertEqual(3, len(selection.delivered))


class FallbackTest(unittest.IsolatedAsyncioTestCase):
    """选片必须永远给得出结果：重排是增益，不是前提。"""

    async def test_a_failing_selector_falls_back_to_hash_order(self) -> None:
        papers = _ranked({"t1": 0.45, "t2": 0.45, "t3": 0.45})
        selector = ExplodingSelector()

        selection = await select_delivery("q", papers, 2, selector)

        self.assertEqual(2, len(selection.delivered))
        self.assertFalse(selection.reranked)
        self.assertIn("RuntimeError", selection.note)

    async def test_an_empty_ranking_falls_back_rather_than_delivering_nothing(
        self,
    ) -> None:
        papers = _ranked({"t1": 0.45, "t2": 0.45, "t3": 0.45})

        selection = await select_delivery("q", papers, 2, FakeSelector([], [], {}))

        self.assertEqual(2, len(selection.delivered))
        self.assertIn("no ranking", selection.note)

    async def test_without_a_selector_behaviour_matches_the_old_hash_order(self) -> None:
        papers = _ranked({"t1": 0.45, "t2": 0.45, "t3": 0.45})

        selection = await select_delivery("q", papers, 2, None)

        self.assertEqual(
            [item.paper_key for item in hash_order(papers)[:2]],
            [item.paper_key for item in selection.delivered],
        )


class ParseSelectionTest(unittest.TestCase):
    def test_a_well_formed_reply_yields_facets_tags_and_order(self) -> None:
        reply = """Here you go:
        {"facets": ["auc", "trees"],
         "settled_facets": {"a": ["auc"]},
         "ranking": [{"id": "t2", "facets": ["trees"], "why": "x"},
                     {"id": "t1", "facets": ["auc"], "why": "y"}]}"""

        facets, tags, order = parse_selection(reply, {"t1", "t2"})

        self.assertEqual(["auc", "trees"], facets)
        self.assertEqual(["t2", "t1"], order)
        self.assertEqual(["trees"], tags["t2"])
        self.assertEqual(["auc"], tags["a"])

    def test_invented_facet_names_are_dropped(self) -> None:
        """放行的话，覆盖度就成了模型自己定义的指标，而不是对课题的覆盖。"""
        reply = '{"facets": ["auc"], "ranking": [{"id": "t1", "facets": ["auc", "made up"]}]}'

        _facets, tags, _order = parse_selection(reply, {"t1"})

        self.assertEqual(["auc"], tags["t1"])

    def test_ids_outside_the_boundary_tier_are_ignored(self) -> None:
        """要么是幻觉，要么指向本来就不参与竞争的论文。"""
        reply = '{"facets": [], "ranking": [{"id": "ghost"}, {"id": "t1"}]}'

        _facets, _tags, order = parse_selection(reply, {"t1"})

        self.assertEqual(["t1"], order)

    def test_a_repeated_id_is_counted_once(self) -> None:
        reply = '{"facets": [], "ranking": [{"id": "t1"}, {"id": "t1"}]}'

        _facets, _tags, order = parse_selection(reply, {"t1"})

        self.assertEqual(["t1"], order)

    def test_malformed_json_yields_nothing_rather_than_half_a_result(self) -> None:
        facets, tags, order = parse_selection("{not json,,}", {"t1"})

        self.assertEqual(([], {}, []), (facets, tags, order))

    def test_a_reply_without_any_json_yields_nothing(self) -> None:
        self.assertEqual(([], {}, []), parse_selection("I refuse.", {"t1"}))


class FillByCoverageTest(unittest.TestCase):
    def test_a_paper_covering_two_facets_is_not_picked_twice(self) -> None:
        ordered = [_paper("t1", 0.45), _paper("t2", 0.45), _paper("t3", 0.45)]
        covered: dict[str, str] = {}

        chosen = fill_by_coverage(
            ordered,
            {"t1": ["a", "b"], "t2": ["a"], "t3": ["c"]},
            covered,
            ["a", "b", "c"],
            2,
        )

        self.assertEqual(["t1", "t3"], [item.paper_key for item in chosen])

    def test_remaining_slots_are_filled_by_rank_once_coverage_is_done(self) -> None:
        ordered = [_paper("t1", 0.45), _paper("t2", 0.45), _paper("t3", 0.45)]

        chosen = fill_by_coverage(ordered, {"t3": ["a"]}, {}, ["a"], 3)

        self.assertEqual(["t3", "t1", "t2"], [item.paper_key for item in chosen])


class FormatPapersTest(unittest.TestCase):
    def test_the_settled_list_carries_no_abstracts(self) -> None:
        """它们只提供覆盖上下文，不参与排序；带摘要只是把 prompt 撑大。"""
        rendered = format_papers([_paper("a", 1.0)], with_abstract=False)

        self.assertIn("a", rendered)
        self.assertNotIn("abstract", rendered)

    def test_an_empty_list_renders_as_none_rather_than_blank(self) -> None:
        self.assertEqual("(none)", format_papers([], with_abstract=True))


class ReproducibilityTest(unittest.TestCase):
    """选片改动的验收指标：同一查询两次交付的重合度。"""

    def test_hash_order_over_a_large_tier_is_stable_across_runs(self) -> None:
        """散列本身是确定的——不可复现来自**池成分**每轮不同，而不是排序随机。

        所以这条指标只有在真机上跑两轮才有意义；这里钉住的是尺子的行为，不是链路的。
        """
        tier = _ranked({f"t{i}": 0.45 for i in range(30)})

        first = [item.paper_key for item in hash_order(tier)[:10]]
        second = [item.paper_key for item in hash_order(tier)[:10]]

        self.assertEqual(1.0, delivery_overlap([first, second]).mean_jaccard)

    def test_a_shared_core_shows_up_as_partial_overlap(self) -> None:
        report = delivery_overlap([["a", "b", "c"], ["a", "b", "d"]])

        self.assertEqual(["a", "b"], report.stable_core)
        self.assertAlmostEqual(0.5, report.mean_jaccard)


class DeliverySelectionTest(unittest.TestCase):
    def test_coverage_is_zero_when_no_facets_were_derived(self) -> None:
        """没有分面时覆盖度无从谈起，报 0 而不是 1——1 会读成"全覆盖"。"""
        self.assertEqual(0.0, DeliverySelection(delivered=[]).coverage())


if __name__ == "__main__":
    unittest.main()

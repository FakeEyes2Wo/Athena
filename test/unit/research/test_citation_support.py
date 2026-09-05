"""引用支持性判定：查内容，而不只是查行为。

核验收紧过两次，两次查的都是**行为**——第一版"这个 id 在不在语料里"，第二版"本轮真的
打开过没有"。一个读过《数据增强综述》再拿它去支持"两两交互特征工程"的 Ideator，两版都
过得去，而那正是真机上实际发生的事。这一层问的是"这段话支持这条主张吗"。
"""

import unittest
from types import SimpleNamespace

from athena.core.research_models import Hypothesis
from athena.research.turns.runner import AgentTurnRunner
from athena.research.idea_generation.citation_support import (
    EVIDENCE_CHARS,
    MAX_EVIDENCE_CHUNKS,
    format_evidence,
    parse_verdict,
)


def _hypothesis(sources: list[str]) -> Hypothesis:
    return Hypothesis(
        statement="pairwise interaction features raise AUC",
        intervention="add f00*f01 products to the feature matrix",
        expected_effect="higher ROC-AUC on the holdout",
        sources=sources,
    )


class ParseVerdictTest(unittest.TestCase):
    def test_an_explicit_yes_is_taken_as_support(self) -> None:
        verdict = parse_verdict("p1", '{"supports": true, "why": "reports the gain"}')

        self.assertTrue(verdict.supports)
        self.assertEqual("reports the gain", verdict.why)

    def test_an_explicit_no_is_taken_as_no_support(self) -> None:
        self.assertFalse(parse_verdict("p1", '{"supports": false}').supports)

    def test_anything_unparseable_counts_as_no_support(self) -> None:
        """误删一条真引用只少一份可追溯性；放行一条假引用会让干预看起来有据可依。"""
        for reply in ("", "I think so?", "{broken", '["not", "an object"]'):
            self.assertFalse(parse_verdict("p1", reply).supports, reply)

    def test_a_non_boolean_truthy_value_is_not_accepted(self) -> None:
        """``"yes"``、``1`` 都不算——只有真正的 true 才算，判据必须是硬的。"""
        self.assertFalse(parse_verdict("p1", '{"supports": "yes"}').supports)
        self.assertFalse(parse_verdict("p1", '{"supports": 1}').supports)


class FormatEvidenceTest(unittest.TestCase):
    def test_only_the_first_few_passages_are_shown(self) -> None:
        """整篇塞进去会把判断稀释成"这篇论文大体上相关吗"——正是要避免的那个问题。"""
        rendered = format_evidence([f"passage {i}" for i in range(10)])

        self.assertIn("passage 0", rendered)
        self.assertNotIn(f"passage {MAX_EVIDENCE_CHUNKS}", rendered)

    def test_long_evidence_is_truncated(self) -> None:
        rendered = format_evidence(["x" * 10_000])

        self.assertLessEqual(len(rendered), EVIDENCE_CHARS + 4)

    def test_reading_nothing_is_said_explicitly(self) -> None:
        """空证据不能渲染成空串——那会让模型以为提示词写坏了。"""
        self.assertIn("no passages", format_evidence([]))
        self.assertIn("no passages", format_evidence(["   "]))


class _Runtime:
    """只带支持性判定这条路径要用到的东西。"""

    def __init__(self, verdicts: dict[str, str], passages: dict[str, list[str]]):
        self._model = "m"
        self.model = "m"
        self._client = None
        self.client = None
        self._verdicts = verdicts
        self._passages = passages
        self.published: list[dict] = []
        self.prompts: list[str] = []

    async def corpus_paper_ids(self) -> set[str]:
        return set(self._passages) | set(self._verdicts)

    def corpus_papers_read(self) -> set[str]:
        return set(self._passages)

    async def corpus_passages_read(self) -> dict[str, list[str]]:
        return dict(self._passages)

    async def publish_output(self, **payload) -> None:
        self.published.append(payload)


class SupportVerificationTest(unittest.IsolatedAsyncioTestCase):
    def _runner(self, runtime: _Runtime) -> AgentTurnRunner:
        runner = AgentTurnRunner(runtime)

        async def fake_chat(prompt, *, model, client=None, **kwargs):
            assert kwargs["config"].max_tokens == 200
            assert kwargs["config"].temperature == 0.1
            runtime.prompts.append(prompt)
            for paper_id, reply in runtime._verdicts.items():
                if f"`{paper_id}`" in prompt:
                    return reply
            return '{"supports": false}'

        import athena.research.turns.support as module

        self._original = module.single_turn_chat
        module.single_turn_chat = fake_chat
        self.addCleanup(setattr, module, "single_turn_chat", self._original)
        return runner

    async def test_a_topically_related_survey_is_dropped(self) -> None:
        """真机形态：《数据增强综述》被引来支持"两两交互特征工程"。"""
        runtime = _Runtime(
            verdicts={
                "doi:survey": '{"supports": false, "why": "a survey of augmentation"}'
            },
            passages={"doi:survey": ["This survey reviews data augmentation methods."]},
        )
        runner = self._runner(runtime)

        kept = await runner._verify_sources([_hypothesis(["doi:survey"])])

        self.assertEqual([], kept[0].sources)
        self.assertTrue(
            any("do not support" in item["text"] for item in runtime.published)
        )

    async def test_a_passage_that_really_backs_the_claim_survives(self) -> None:
        runtime = _Runtime(
            verdicts={"arxiv:1": '{"supports": true, "why": "reports the same gain"}'},
            passages={"arxiv:1": ["Adding pairwise products raised AUC by 0.03."]},
        )
        runner = self._runner(runtime)

        kept = await runner._verify_sources([_hypothesis(["arxiv:1"])])

        self.assertEqual(["arxiv:1"], kept[0].sources)

    async def test_only_the_unsupported_citation_is_removed(self) -> None:
        runtime = _Runtime(
            verdicts={
                "arxiv:good": '{"supports": true}',
                "doi:bad": '{"supports": false, "why": "different intervention"}',
            },
            passages={"arxiv:good": ["evidence"], "doi:bad": ["unrelated"]},
        )
        runner = self._runner(runtime)

        kept = await runner._verify_sources([_hypothesis(["arxiv:good", "doi:bad"])])

        self.assertEqual(["arxiv:good"], kept[0].sources)

    async def test_the_claim_and_the_intervention_both_reach_the_judge(self) -> None:
        """只给主张不给干预，判定就退化成"这篇论文和这个话题相关吗"。"""
        runtime = _Runtime(
            verdicts={"arxiv:1": '{"supports": true}'},
            passages={"arxiv:1": ["evidence"]},
        )
        runner = self._runner(runtime)

        await runner._verify_sources([_hypothesis(["arxiv:1"])])

        self.assertIn("pairwise interaction features raise AUC", runtime.prompts[0])
        self.assertIn("add f00*f01 products", runtime.prompts[0])

    async def test_a_failing_judge_leaves_the_citations_alone(self) -> None:
        """整层不可用是增益消失，不该把已经通过前一关的结论一起清掉。"""
        runtime = _Runtime(verdicts={}, passages={"arxiv:1": ["evidence"]})
        runner = AgentTurnRunner(runtime)
        import athena.research.turns.support as module

        async def exploding(*_args, **_kwargs):
            raise RuntimeError("endpoint down")

        original = module.single_turn_chat
        module.single_turn_chat = exploding
        self.addCleanup(setattr, module, "single_turn_chat", original)

        kept = await runner._verify_sources([_hypothesis(["arxiv:1"])])

        self.assertEqual(["arxiv:1"], kept[0].sources)

    async def test_a_hypothesis_without_citations_costs_no_call(self) -> None:
        runtime = _Runtime(verdicts={}, passages={})
        runner = AgentTurnRunner(runtime)

        kept = await runner._verify_sources([_hypothesis([])])

        self.assertEqual([], kept[0].sources)
        self.assertEqual([], runtime.prompts)


if __name__ == "__main__":
    unittest.main()

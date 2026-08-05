"""Unit tests for the multi-strategy parallel candidate generation and dedup."""

import unittest

from athena.workflows.search.candidate_generation import (
    GENERATION_STRATEGIES,
    MAX_VERBALIZED_SAMPLES,
    deduplicate_candidates,
    generate_candidates,
    generate_one_strategy,
)
from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    HypothesisDraft,
    HypothesisPackage,
    ResearchProblemInput,
)
from unit.fakes import make_routed_model


def _problem() -> ResearchProblemInput:
    return ResearchProblemInput(
        question="Does X affect Y?", domain="biology", objective="find a mechanism",
        evidence_texts=["X correlates with Y in mice."],
    )


def _draft(statement: str, probability: float = 1.0) -> HypothesisDraft:
    return HypothesisDraft(
        statement=statement, intervention="knock out X", expected_effect="Y decreases",
        generation_strategy="", sampling_probability=probability,
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[], predicted_observations=["Y decreases"],
        disconfirming_observations=["Y stays flat"],
    )


def _strategy_route(index: int) -> str:
    # 不带结尾的 \n：pydantic-ai 的 str(messages) 把换行转成字面 "\n" 转义序列，真实换行符
    # 反而匹配不上（同一个坑在 review_board 的 _ROUTE_REVIEW_* 常量上已经踩过一次）
    return f"Generation strategy: {GENERATION_STRATEGIES[index].strategy_id}"


def _package(novel_hypothesis: str, probability: float = 1.0, idea_id: str = "idea-1") -> HypothesisPackage:
    return HypothesisPackage(
        idea_id=idea_id, generation_strategy="test_strategy", novel_hypothesis=novel_hypothesis,
        sampling_probability=probability, supported_premises=[], inference_chain=[],
        predicted_observations=["p"], disconfirming_observations=["d"], lineage_op="generate",
    )


class GenerateOneStrategyTest(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_strategy_id_when_draft_leaves_it_blank(self) -> None:
        model = make_routed_model({_strategy_route(0): _draft("X causes Y")})
        package = await generate_one_strategy(_problem(), [], GENERATION_STRATEGIES[0], model=model)
        self.assertEqual("analogical_transfer", package.generation_strategy)

    async def test_does_not_ask_the_model_to_self_assess_probability(self) -> None:
        # sampling_probability 默认 1.0，_draft() 也默认 1.0——这条断言在"策略不该自评分"
        # 这件事上是弱信号，真正的把关在 prompts.py 的 system prompt 文本里，这里只确认
        # 默认值原样透传，没有被生成侧悄悄覆盖成别的东西
        model = make_routed_model({_strategy_route(1): _draft("X causes Y")})
        package = await generate_one_strategy(_problem(), [], GENERATION_STRATEGIES[1], model=model)
        self.assertEqual(1.0, package.sampling_probability)


class GenerateCandidatesTest(unittest.IsolatedAsyncioTestCase):
    async def test_runs_exactly_sample_size_strategies_in_parallel(self) -> None:
        # 每个策略绑一条独立路由；make_routed_model 要求命中数恰好为 1，缺一条都会
        # AssertionError——这条测试本身就在断言"确实起了 sample_size 个并发策略调用"
        routes = {
            _strategy_route(i): _draft(f"hypothesis-{i}") for i in range(3)
        }
        packages = await generate_candidates(_problem(), [], sample_size=3, model=make_routed_model(routes))
        self.assertEqual(3, len(packages))

    async def test_each_package_is_tagged_with_its_own_strategy(self) -> None:
        # 把它对应的生产代码删掉（比如把 generate_one_strategy 里传给每个调用的 strategy
        # 都写死成 GENERATION_STRATEGIES[0]），这条测试会失败：5 个包的 generation_strategy
        # 会全部塌缩成同一个值，而不是跟策略一一对应
        routes = {
            _strategy_route(i): _draft(f"hypothesis-{i}") for i in range(len(GENERATION_STRATEGIES))
        }
        packages = await generate_candidates(
            _problem(), [], sample_size=len(GENERATION_STRATEGIES), model=make_routed_model(routes))
        self.assertEqual(
            {s.strategy_id for s in GENERATION_STRATEGIES},
            {p.generation_strategy for p in packages},
        )

    async def test_one_strategy_failing_does_not_drag_down_the_others(self) -> None:
        routes = {
            _strategy_route(0): RuntimeError("provider hiccup"),
            _strategy_route(1): _draft("X causes Y"),
            _strategy_route(2): _draft("Z inhibits W"),
        }
        packages = await generate_candidates(_problem(), [], sample_size=3, model=make_routed_model(routes))
        self.assertEqual(2, len(packages))
        self.assertEqual({"X causes Y", "Z inhibits W"}, {p.novel_hypothesis for p in packages})

    async def test_all_strategies_failing_returns_empty_list_not_an_error(self) -> None:
        routes = {_strategy_route(0): RuntimeError("provider hiccup")}
        packages = await generate_candidates(_problem(), [], sample_size=1, model=make_routed_model(routes))
        self.assertEqual([], packages)

    async def test_assigns_unique_idea_ids(self) -> None:
        routes = {
            _strategy_route(0): _draft("X causes Y"),
            _strategy_route(1): _draft("X inhibits Y"),
        }
        packages = await generate_candidates(_problem(), [], sample_size=2, model=make_routed_model(routes))
        self.assertNotEqual(packages[0].idea_id, packages[1].idea_id)

    async def test_rejects_sample_size_above_maximum(self) -> None:
        with self.assertRaises(ValueError):
            await generate_candidates(_problem(), [], sample_size=MAX_VERBALIZED_SAMPLES + 1)

    async def test_rejects_sample_size_below_one(self) -> None:
        with self.assertRaises(ValueError):
            await generate_candidates(_problem(), [], sample_size=0)


class DeduplicateCandidatesTest(unittest.TestCase):
    def test_near_duplicate_statements_collapse_to_one(self) -> None:
        packages = [_package("X causes Y in mice", 0.9), _package("X causes Y in mice indeed", 0.5, "idea-2")]
        kept = deduplicate_candidates(packages)
        self.assertEqual(1, len(kept))

    def test_keeps_the_higher_probability_candidate(self) -> None:
        packages = [_package("X causes Y in mice", 0.4, "idea-1"), _package("X causes Y in mice", 0.9, "idea-2")]
        kept = deduplicate_candidates(packages)
        self.assertEqual(1, len(kept))
        self.assertEqual("idea-2", kept[0].idea_id)

    def test_distinct_statements_are_both_kept(self) -> None:
        packages = [_package("X causes Y", 0.5, "idea-1"), _package("Z inhibits W", 0.5, "idea-2")]
        kept = deduplicate_candidates(packages)
        self.assertEqual(2, len(kept))

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual([], deduplicate_candidates([]))

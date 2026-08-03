"""Unit tests for the REVISE debate loop."""

import tempfile
import unittest

from pydantic_ai.models.function import FunctionModel

from athena.core.agent import Agent, AgentConfig, StreamEvent
from athena.core.tool import ToolRegistry
from athena.storage.artifact_store import LocalArtifactStore
from athena.workflows.search.evidence_retrieval import build_novelty_question
from athena.workflows.search.gatekeeper import MAX_TOLERATED_RISKS
from athena.workflows.search.idea_schemas import (
    ClaimEvidence, ClaimRole, FalsifiabilityJudgment, GATE_RUBRIC_VERSION, GateDecision,
    GateVerdict, HypothesisPackage, NoveltyEvidenceJudgment, NoveltyEvidenceReport,
    RevisionDraft, SkepticJudgment, SkepticReport,
)
from athena.workflows.search.review_board import REVIEW_PERSPECTIVES, build_perspective_input
from athena.workflows.search.revision import (
    MAX_DEBATE_ROUNDS, build_revision_prompt, is_no_op_revision, is_revisable,
    novelty_is_stale, refresh_stale_evidence, revise_candidate, run_debate,
    select_debate_opponent, stale_perspectives,
)
from unit.fakes import make_routed_model, tool_call_response

_FAKE_CORPUS_REF = "sha256:" + "c" * 64


def _package(sampling_probability: float = 0.42) -> HypothesisPackage:
    return HypothesisPackage(
        idea_id="idea-1", generation_strategy="s", novel_hypothesis="X causes Y",
        sampling_probability=sampling_probability,
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[], predicted_observations=["Y increases"],
        disconfirming_observations=["Y flat"], lineage_op="generate",
    )


def _reviews() -> list[SkepticReport]:
    return [
        SkepticReport(idea_id="idea-1", perspective="methodology", critique="no control arm",
                      unaddressed_risks=["confound"], fatal_flaw_found=False),
        SkepticReport(idea_id="idea-1", perspective="statistics", critique="underpowered",
                      unaddressed_risks=["n too small"], fatal_flaw_found=False),
        SkepticReport(idea_id="idea-1", perspective="domain_consistency",
                      critique="mechanism plausible", unaddressed_risks=[], fatal_flaw_found=False),
    ]


def _draft() -> RevisionDraft:
    return RevisionDraft(
        rebuttal="a matched control arm is now specified",
        changes_made=["added control arm"],
        revised_novel_hypothesis="X causes Y relative to a matched control",
        revised_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        revised_predicted_observations=["Y increases versus control"],
        revised_disconfirming_observations=["Y flat versus control"],
    )


async def _novelty(store: LocalArtifactStore, package: HypothesisPackage) -> NoveltyEvidenceReport:
    # 模块级 helper（而非 StalenessTest 的方法）：Task 9 的测试需要复用同一份构造逻辑。
    return NoveltyEvidenceReport(
        idea_id="idea-1", nearest_work=[], facet_overlap={"problem": 0.1},
        coverage_ref=_FAKE_CORPUS_REF, temporal_ref=_FAKE_CORPUS_REF,
        query_log_ref=await store.put_text("old transcript"),
        input_ref=await store.put_text(build_novelty_question(package, _FAKE_CORPUS_REF)),
        uncertainty=0.2,
    )


async def _reviews_with_refs(
    store: LocalArtifactStore, package: HypothesisPackage
) -> list[SkepticReport]:
    # 模块级 helper：与 _novelty 同理，供 Task 9 直接复用。
    out = []
    for perspective in REVIEW_PERSPECTIVES:
        prompt = build_perspective_input(
            package, perspective, corpus_ref=_FAKE_CORPUS_REF,
            prior_transcript="old transcript")
        out.append(SkepticReport(
            idea_id="idea-1", perspective=perspective.perspective_id, critique="c",
            unaddressed_risks=[], fatal_flaw_found=False,
            input_ref=await store.put_text(prompt),
        ))
    return out


def _decision(verdict: GateVerdict, blocking_factor: str | None) -> GateDecision:
    return GateDecision(idea_id="idea-1", gate_phase="full", verdict=verdict,
                        rubric_version=GATE_RUBRIC_VERSION, item_scores=[],
                        blocking_factor=blocking_factor)


class RevisionEligibilityTest(unittest.TestCase):
    def test_risk_items_are_revisable(self) -> None:
        self.assertTrue(is_revisable(_decision(GateVerdict.REVISE, "risk_ok_methodology")))
        self.assertTrue(is_revisable(_decision(GateVerdict.REVISE, "risk_total")))

    def test_novelty_ok_is_excluded(self) -> None:
        # 该分支只由 facet_overlap 为空触发 = 检索侧失败，改假设文本不会让检索恢复
        self.assertFalse(is_revisable(_decision(GateVerdict.REVISE, "novelty_ok")))

    def test_non_revise_verdicts_are_excluded(self) -> None:
        for verdict in (GateVerdict.PASS, GateVerdict.REJECT, GateVerdict.EXPLORATORY):
            self.assertFalse(is_revisable(_decision(verdict, "risk_ok_methodology")))


class DebateOpponentTest(unittest.TestCase):
    def test_risk_ok_item_names_its_own_opponent(self) -> None:
        self.assertEqual("statistics",
                         select_debate_opponent("risk_ok_statistics", _reviews()))

    def test_risk_total_picks_the_noisiest_perspective(self) -> None:
        reviews = [
            SkepticReport(idea_id="idea-1", perspective="methodology", critique="c",
                          unaddressed_risks=["a"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="statistics", critique="c",
                          unaddressed_risks=["a", "b", "c"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="domain_consistency", critique="c",
                          unaddressed_risks=["a"], fatal_flaw_found=False),
        ]
        self.assertEqual("statistics", select_debate_opponent("risk_total", reviews))

    def test_ties_break_by_review_perspectives_order_not_caller_order(self) -> None:
        # 确定性：同样输入必须选出同一个对手。刻意用倒序传入，证明函数内部按
        # REVIEW_PERSPECTIVES 重排，而不是听凭调用方（gather 入参顺序）的顺序。
        tied = [
            SkepticReport(idea_id="idea-1", perspective="domain_consistency", critique="c",
                          unaddressed_risks=["a", "b"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="statistics", critique="c",
                          unaddressed_risks=["a", "b"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="methodology", critique="c",
                          unaddressed_risks=["a", "b"], fatal_flaw_found=False),
        ]
        self.assertEqual("methodology", select_debate_opponent("risk_total", tied))


class NoOpGuardTest(unittest.TestCase):
    def test_identical_content_is_a_no_op(self) -> None:
        same = RevisionDraft(
            rebuttal="nothing really changed", changes_made=[],
            revised_novel_hypothesis=_package().novel_hypothesis,
            revised_premises=_package().supported_premises,
            revised_predicted_observations=_package().predicted_observations,
            revised_disconfirming_observations=_package().disconfirming_observations,
        )
        self.assertTrue(is_no_op_revision(same, _package()))

    def test_disconfirmer_only_change_is_NOT_a_no_op(self) -> None:
        # 判定域必须是四个 revised_* 字段，不是 novel_hypothesis 的相似度。按后者判定，
        # 最典型的修订（只补 disconfirmer，假设文本一字未动）会被当成噪声掐死，
        # 对手一次都不会被叫到，主路径直接失效。
        draft = RevisionDraft(
            rebuttal="added an explicit disconfirmer", changes_made=["added disconfirmer"],
            revised_novel_hypothesis=_package().novel_hypothesis,
            revised_premises=_package().supported_premises,
            revised_predicted_observations=_package().predicted_observations,
            revised_disconfirming_observations=["Y flat versus matched control"],
        )
        self.assertFalse(is_no_op_revision(draft, _package()))

    def test_premise_only_change_is_NOT_a_no_op(self) -> None:
        draft = RevisionDraft(
            rebuttal="bound a second evidence ref", changes_made=["added ref"],
            revised_novel_hypothesis=_package().novel_hypothesis,
            revised_premises=[ClaimEvidence(claim="X correlates with Y",
                                            role=ClaimRole.SUPPORTED_PREMISE,
                                            supporting_refs=["ev-0", "ev-1"])],
            revised_predicted_observations=_package().predicted_observations,
            revised_disconfirming_observations=_package().disconfirming_observations,
        )
        self.assertFalse(is_no_op_revision(draft, _package()))


class RevisionPromptTest(unittest.TestCase):
    def test_prompt_carries_blocking_item_and_all_three_critiques(self) -> None:
        prompt = build_revision_prompt(
            _package(), blocking_factor="risk_ok_methodology",
            debated_perspective="methodology", reviews=_reviews(), prior_rounds=[])
        self.assertIn("risk_ok_methodology", prompt)
        self.assertIn("no control arm", prompt)      # 被拦视角
        self.assertIn("underpowered", prompt)        # 另外两个视角，防"修好 A 弄坏 B"
        self.assertIn("mechanism plausible", prompt)

    def test_prompt_never_leaks_sampling_probability(self) -> None:
        # 泄漏面一：prompt。reviser 全程看不到这个数，rebuttal 才不可能复述它
        prompt = build_revision_prompt(
            _package(sampling_probability=0.42), blocking_factor="risk_ok_methodology",
            debated_perspective="methodology", reviews=_reviews(), prior_rounds=[])
        self.assertNotIn("0.42", prompt)
        self.assertNotIn("sampling_probability", prompt)


class ReviseCandidateTest(unittest.IsolatedAsyncioTestCase):
    async def test_revised_package_keeps_id_and_probability_bumps_round(self) -> None:
        model = make_routed_model({"Blocking rubric item:": _draft()})
        revised, draft = await revise_candidate(
            _package(), blocking_factor="risk_ok_methodology",
            debated_perspective="methodology", reviews=_reviews(), prior_rounds=[], model=model)
        self.assertEqual("idea-1", revised.idea_id)             # hard_gate 要求全套报告同 id
        self.assertEqual(0.42, revised.sampling_probability)    # 代码搬运，不向 LLM 索要
        self.assertEqual(1, revised.revision_round)
        self.assertEqual("revise", revised.lineage_op)
        self.assertEqual("X causes Y relative to a matched control", revised.novel_hypothesis)
        self.assertEqual("a matched control arm is now specified", draft.rebuttal)

    async def test_invalid_revision_raises_and_validator_is_not_relaxed(self) -> None:
        # 空 disconfirmers 构造不出合法 HypothesisPackage —— 这是预期路径，按 reviser 失败处理，
        # 绝不放宽校验器（那两条不变量是 evidence_traceable 这项 rubric 的结构基础）
        bad = _draft().model_copy(update={"revised_disconfirming_observations": []})
        model = make_routed_model({"Blocking rubric item:": bad})
        with self.assertRaises(ValueError):
            await revise_candidate(
                _package(), blocking_factor="risk_ok_methodology",
                debated_perspective="methodology", reviews=_reviews(), prior_rounds=[], model=model)


class RevisionRetryTest(unittest.IsolatedAsyncioTestCase):
    """MAX_REVISION_ATTEMPTS 在核心流程跑通之后接进 revise_candidate 的重试：单次瞬时失败
    重试一次即可恢复；重试次数耗尽仍按原样上抛，run_debate 的"候选原样返回"承诺不受影响。"""

    async def test_transient_failure_is_retried_once(self) -> None:
        calls = {"n": 0}

        def flaky(messages, info):
            # 第一次调用模拟 provider 瞬时抖动；第二次调用正常产出——make_routed_model
            # 只能给固定响应，表达不出"先失败再成功"，这里改用手写计数器闭包的 FunctionModel。
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return tool_call_response(_draft(), info)

        revised, _draft_ = await revise_candidate(
            _package(), blocking_factor="risk_ok_methodology",
            debated_perspective="methodology", reviews=_reviews(), prior_rounds=[],
            model=FunctionModel(flaky))
        self.assertEqual(2, calls["n"])
        self.assertEqual(1, revised.revision_round)

    async def test_exhausted_retries_still_raise(self) -> None:
        model = make_routed_model({"Blocking rubric item:": RuntimeError("down")})
        with self.assertRaises(Exception):
            await revise_candidate(
                _package(), blocking_factor="risk_ok_methodology",
                debated_perspective="methodology", reviews=_reviews(), prior_rounds=[],
                model=model)


class StalenessTest(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_package_is_not_stale_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            self.assertFalse(await novelty_is_stale(
                package, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(set(), await stale_perspectives(
                package, reviews, artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="old transcript"))

    async def test_changed_hypothesis_makes_novelty_and_all_perspectives_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(update={"novel_hypothesis": "Z causes Y"})
            self.assertTrue(await novelty_is_stale(
                revised, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(
                {"methodology", "statistics", "domain_consistency"},
                await stale_perspectives(revised, reviews, artifacts=store,
                                         corpus_ref=_FAKE_CORPUS_REF,
                                         prior_transcript="old transcript"))

    async def test_disconfirmer_only_revision_spares_both_retrieval_reports(self) -> None:
        # 成本论证的支点：典型修订必须**证明性地**不触发 novelty 与 domain_consistency，
        # 那是仅有的两个 retrieval loop。这条一旦失守，整轮设计的成本前提就没了。
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(
                update={"disconfirming_observations": ["Y flat versus matched control"]})
            self.assertFalse(await novelty_is_stale(
                revised, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(
                {"methodology", "statistics"},
                await stale_perspectives(revised, reviews, artifacts=store,
                                         corpus_ref=_FAKE_CORPUS_REF,
                                         prior_transcript="old transcript"))

    async def test_new_transcript_cascades_to_domain_consistency(self) -> None:
        # 级联：修订只改 predicted_observations，domain_consistency 自身字段（novel_hypothesis）
        # 没变，但 novelty 重跑换了转录，它必须跟着 stale。
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(update={"predicted_observations": ["Y doubles"]})
            stale = await stale_perspectives(
                revised, reviews, artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="freshly retrieved transcript")   # 刷新后的转录
            self.assertIn("domain_consistency", stale)

    async def test_missing_fingerprint_is_always_stale(self) -> None:
        # fail-closed：失败降级的报告没有可信指纹，无法证明未失效就不能复用
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = (await _novelty(store, package)).model_copy(
                update={"input_ref": None})
            reviews = [r.model_copy(update={"input_ref": None})
                       for r in await _reviews_with_refs(store, package)]
            self.assertTrue(await novelty_is_stale(
                package, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(
                {"methodology", "statistics", "domain_consistency"},
                await stale_perspectives(package, reviews, artifacts=store,
                                         corpus_ref=_FAKE_CORPUS_REF,
                                         prior_transcript="old transcript"))


class RunDebateTest(unittest.IsolatedAsyncioTestCase):
    def _routes(self, *, cleared: bool) -> dict:
        # 未清除分支必须严格超过 MAX_TOLERATED_RISKS（perspective_ok 的容忍上限），否则单条
        # 未处理风险仍会被判定为通过，"never clearing" 场景就名不副实——直接引用常量，不用
        # 魔法数字，阈值调整时这条 fixture 自动跟着变。
        # 两轮 reviser 各给一份不同的修订：make_routed_model 不按调用顺序消费，若两轮路由到
        # 同一份固定 RevisionDraft，第二轮的"修订"内容会与第一轮已经落地的当前稿逐字段相同，
        # is_no_op_revision 判它是空转而提前结束循环，"跑满 MAX_DEBATE_ROUNDS" 的断言就假不成立。
        # 用 reviser prompt 里只在各自那一轮出现的锚点文本区分路由："(this is the first round)"
        # 只在第一轮的 prior_rounds 为空时出现；"reviewer replied=re-reviewed" 只在第二轮才会
        # 出现在上一轮的 prior_rounds 摘要里。
        risks = [] if cleared else [f"unaddressed risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
        second_round_draft = RevisionDraft(
            rebuttal="a second, independently replicated cohort is now cited",
            changes_made=["cited a second independent cohort"],
            revised_novel_hypothesis="X causes Y relative to a matched control, replicated twice",
            revised_premises=_draft().revised_premises,
            revised_predicted_observations=_draft().revised_predicted_observations,
            revised_disconfirming_observations=_draft().revised_disconfirming_observations,
        )
        return {
            "(this is the first round)": _draft(),
            "reviewer replied=re-reviewed": second_round_draft,
            "Debate round": SkepticJudgment(critique="re-reviewed", unaddressed_risks=risks,
                                            fatal_flaw_found=False),
        }

    async def test_cleared_debate_stops_after_one_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package, _reviews_ = _package(), _reviews()
            final, reviews, rounds = await run_debate(
                package, blocking_factor="risk_ok_methodology", reviews=_reviews_,
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="old transcript",
                model=make_routed_model(self._routes(cleared=True)))
            self.assertEqual(1, len(rounds))
            self.assertTrue(rounds[0].cleared)
            self.assertEqual("methodology", rounds[0].debated_perspective)
            self.assertEqual(1, final.revision_round)
            # 被辩视角的报告被换成了辩论产出的那份
            methodology = next(r for r in reviews if r.perspective == "methodology")
            self.assertEqual("re-reviewed", methodology.critique)

    async def test_never_clearing_debate_stops_at_max_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final, _reviews_, rounds = await run_debate(
                _package(), blocking_factor="risk_ok_methodology", reviews=_reviews(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="old transcript",
                model=make_routed_model(self._routes(cleared=False)))
            self.assertEqual(MAX_DEBATE_ROUNDS, len(rounds))
            self.assertFalse(rounds[-1].cleared)
            self.assertEqual(MAX_DEBATE_ROUNDS, final.revision_round)

    async def test_debate_report_records_canonical_not_debate_prompt_fingerprint(self) -> None:
        # 设计 §4.4：存规范化输入指纹，使被辩视角在终局天然复用
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalArtifactStore(tmp)
            final, reviews, _rounds = await run_debate(
                _package(), blocking_factor="risk_ok_methodology", reviews=_reviews(),
                artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="old transcript",
                model=make_routed_model(self._routes(cleared=True)))
            self.assertEqual(
                set(),
                await stale_perspectives(final, [r for r in reviews
                                                 if r.perspective == "methodology"] +
                                         await self._fresh_others(store, final),
                                         artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                                         prior_transcript="old transcript"))

    async def _fresh_others(self, store, package) -> list[SkepticReport]:
        out = []
        for perspective in REVIEW_PERSPECTIVES:
            if perspective.perspective_id == "methodology":
                continue
            prompt = build_perspective_input(package, perspective,
                                             corpus_ref=_FAKE_CORPUS_REF,
                                             prior_transcript="old transcript")
            out.append(SkepticReport(
                idea_id="idea-1", perspective=perspective.perspective_id, critique="c",
                unaddressed_risks=[], fatal_flaw_found=False,
                input_ref=await store.put_text(prompt)))
        return out

    async def test_no_op_revision_exits_without_calling_the_opponent(self) -> None:
        no_op = RevisionDraft(
            rebuttal="no change", changes_made=[],
            revised_novel_hypothesis=_package().novel_hypothesis,
            revised_premises=_package().supported_premises,
            revised_predicted_observations=_package().predicted_observations,
            revised_disconfirming_observations=_package().disconfirming_observations,
        )
        # 路由里**不放** "Debate round" —— 对手一旦被调用，make_routed_model 会因 0 命中抛
        # AssertionError，这就是"对手 0 次调用"的强断言
        with tempfile.TemporaryDirectory() as tmp:
            final, _reviews_, rounds = await run_debate(
                _package(), blocking_factor="risk_ok_methodology", reviews=_reviews(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="old transcript",
                model=make_routed_model({"Blocking rubric item:": no_op}))
            self.assertEqual(1, len(rounds))
            self.assertFalse(rounds[0].cleared)
            self.assertIsNone(rounds[0].reviewer_response_ref)
            self.assertEqual(0, final.revision_round)   # 无实质改动，不算一轮修订

    async def test_reviser_failure_leaves_the_package_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final, reviews, rounds = await run_debate(
                _package(), blocking_factor="risk_ok_methodology", reviews=_reviews(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="old transcript",
                model=make_routed_model({"Blocking rubric item:": RuntimeError("down")}))
            self.assertEqual(_package(), final)          # 逐字段相同
            self.assertEqual(_reviews(), reviews)
            self.assertEqual([], rounds)


class _StaticTextProvider:
    """总是回放固定文本、不调用工具的假 provider；记下调用次数，供测试断言检索循环有没有
    真的被触发。照抄 test_review_board.py 的实现（同一份假 provider 没有理由写两遍）。默认
    文本不含任何路由 key 的子串，避免假 provider 的回放文本被 make_routed_model 误路由。
    """

    def __init__(self, text: str = "no contradicting work found") -> None:
        self._text = text
        self.calls = 0

    async def stream(self, config, messages, cancel):
        self.calls += 1
        yield StreamEvent("text_delta", {"delta": self._text, "accumulated": self._text})
        yield StreamEvent("response_completed")


def _build_agent(provider: _StaticTextProvider) -> Agent:
    # 检索 Agent 工厂：绑定假 provider，供 refresh_stale_evidence 的 novelty_agent/
    # domain_review_agent 参数使用。
    agent = Agent(AgentConfig(model="test-model", system_prompt="system", tools=ToolRegistry()))
    agent._provider = provider
    return agent


def _refresh_routes() -> dict:
    # 覆盖终局刷新可能触发的五类单轮调用锚点：novelty summary、三个视角、falsifiability。
    # 锚点互不为子串，且都不出现在 _StaticTextProvider 的默认回放文本里。
    return {
        "novelty and temporal-integrity assessment": NoveltyEvidenceJudgment(
            nearest_work=[], facet_overlap={"problem": 0.1}, coverage_estimate=0.5,
            unrecalled_risk=0.2, citation_cutoff_ok=True, retrieval_cutoff_ok=True,
            post_cutoff_similarity=0.1, possible_memorization=False, leakage_risk=0.1,
            historical_backtest_validity=True, uncertainty=0.2,
        ),
        "Review perspective: methodology": SkepticJudgment(
            critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        "Review perspective: statistics": SkepticJudgment(
            critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        "Review perspective: domain_consistency": SkepticJudgment(
            critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        "falsifiability auditor": FalsifiabilityJudgment(
            testable_implication="t", unobservable_variables=[], is_falsifiable=True),
    }


class RefreshStaleEvidenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_package_reruns_no_retrieval_at_all(self) -> None:
        # 成本论证的端到端佐证：没有输入变化就不该有任何检索循环
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty_provider, domain_provider = _StaticTextProvider(), _StaticTextProvider()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            await refresh_stale_evidence(
                package, problem_domain="ai4s", novelty=novelty, reviews=reviews,
                novelty_agent=_build_agent(novelty_provider),
                domain_review_agent=_build_agent(domain_provider),
                artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_refresh_routes()),
            )
            self.assertEqual(0, novelty_provider.calls)
            self.assertEqual(0, domain_provider.calls)

    async def test_changed_hypothesis_reruns_both_retrieval_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty_provider, domain_provider = _StaticTextProvider(), _StaticTextProvider()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(update={"novel_hypothesis": "Z causes Y"})
            await refresh_stale_evidence(
                revised, problem_domain="ai4s", novelty=novelty, reviews=reviews,
                novelty_agent=_build_agent(novelty_provider),
                domain_review_agent=_build_agent(domain_provider),
                artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_refresh_routes()),
            )
            self.assertEqual(1, novelty_provider.calls)
            self.assertEqual(1, domain_provider.calls)

    async def test_predicted_observations_only_change_makes_domain_stale_via_new_transcript(
        self,
    ) -> None:
        # 顺序论证真正吃到"先 novelty、再视角"这条规则的场景：novel_hypothesis 不变，只改
        # predicted_observations。该字段只出现在 build_novelty_question 里（触发 novelty
        # 单独重跑、换新转录），不出现在 DOMAIN_CONSISTENCY_QUESTION_TEMPLATE 里（该模板只嵌
        # novel_hypothesis + corpus_ref + prior_retrieval）。domain_consistency 的 staleness
        # 因此**只能**靠"新转录 != 旧转录"这一条路径成立——上面两条用例都改了
        # novel_hypothesis，novel_hypothesis 本身就直接嵌在 domain 的输入模板里，域一致性视角
        # 会在自己字段上就判 stale，与转录是新是旧无关，测不出顺序颠倒的回归。这条用例把
        # novel_hypothesis 钉死不变，如果 revision.py 把"先 novelty 再读转录判视角"两句顺序
        # 颠倒（或者拿旧转录判视角），domain_consistency 会用旧转录判定，结果是不 stale，
        # domain_provider 永远不会被调用，下面的断言会失败在 domain_provider.calls == 0。
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty_provider, domain_provider = _StaticTextProvider(), _StaticTextProvider()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(
                update={"predicted_observations": ["Y increases sharply"]})
            await refresh_stale_evidence(
                revised, problem_domain="ai4s", novelty=novelty, reviews=reviews,
                novelty_agent=_build_agent(novelty_provider),
                domain_review_agent=_build_agent(domain_provider),
                artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_refresh_routes()),
            )
            self.assertEqual(1, novelty_provider.calls)
            self.assertEqual(1, domain_provider.calls)


class PromptConstantCollisionTest(unittest.TestCase):
    """回归护栏：REVIEW_PERSPECTIVE_HEADER_TEMPLATE / DOMAIN_CONSISTENCY_* 三处硬编码了
    "Review perspective: " 前缀，make_routed_model 要求命中数恰好为 1。若修订/辩论侧的四个
    prompt 常量也带上这串文本，会在同一份 prompt 里造成 2 处命中，测试双工具直接抛
    AssertionError。这条测试把这个前提钉死，防止未来编辑悄悄引入。"""

    def test_revision_and_debate_prompts_never_carry_the_review_header(self) -> None:
        from athena.workflows.prompts import (
            DEBATE_REREVIEW_PROMPT_TEMPLATE,
            DEBATE_REREVIEW_SYSTEM_PROMPT,
            REVISER_SYSTEM_PROMPT,
            REVISION_USER_PROMPT_TEMPLATE,
        )
        for constant in (REVISER_SYSTEM_PROMPT, REVISION_USER_PROMPT_TEMPLATE,
                         DEBATE_REREVIEW_SYSTEM_PROMPT, DEBATE_REREVIEW_PROMPT_TEMPLATE):
            self.assertNotIn("Review perspective: ", constant)

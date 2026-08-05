"""图/状态编排的单元测试。

第一组测试刻意只验证 langgraph 的 API 形状本身——本模块的图装配建立在这些行为上，
它们若在升级中变了，这里要第一个红，而不是让整张图在别处诡异地错。
"""

import asyncio
import operator
import tempfile
import unittest
from dataclasses import dataclass
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import get_runtime
from langgraph.types import Send

from athena.research.ranking import PairwiseComparison
from athena.workflows.search.graph import run_graph
from athena.workflows.search.idea_schemas import (
    GATE_RUBRIC_VERSION, ClaimEvidence, ClaimRole, FalsifiabilityReport, GateDecision, GateVerdict,
    HypothesisPackage, PipelineCandidateResult, ResearchProblemInput, RevisionRound,
    StructuralCheckReport,
)
from athena.workflows.search.state import PipelineDeps, PipelineState


_FAKE_CORPUS_REF = "sha256:" + "c" * 64


def _decision(verdict: GateVerdict, blocking_factor: str | None) -> GateDecision:
    """照抄 test_revision.py 里的同名 helper——两处都只是想要一个不关心 item_scores 的
    GateDecision 桩，没有必要共享一份定义。"""
    return GateDecision(idea_id="idea-1", gate_phase="full", verdict=verdict,
                        rubric_version=GATE_RUBRIC_VERSION, item_scores=[],
                        blocking_factor=blocking_factor)


def _pkg() -> HypothesisPackage:
    """照抄 test_revision.py::_package 的最小构造——路由测试不关心具体字段取值。"""
    return HypothesisPackage(
        idea_id="idea-1", generation_strategy="s", novel_hypothesis="X causes Y",
        sampling_probability=0.42,
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[], predicted_observations=["Y increases"],
        disconfirming_observations=["Y flat"], lineage_op="generate",
    )


def _round(n: int) -> RevisionRound:
    """最小 RevisionRound 桩，只关心 round_index——供 route_after_rereview 数轮次。"""
    return RevisionRound(
        round_index=n, debated_perspective="methodology",
        package_ref="sha256:" + "a" * 64, rebuttal_ref="sha256:" + "b" * 64, cleared=False,
    )


def _pipeline_result(idea_id: str) -> PipelineCandidateResult:
    """最小 PipelineCandidateResult 桩，仿 test_idea_schemas.py::_pipeline_result 的写法——
    只关心 idea_id 是否能在 collect_node 重排后区分出候选，不关心其余字段取值。"""
    return PipelineCandidateResult(
        package=HypothesisPackage(
            idea_id=idea_id, generation_strategy="s", novel_hypothesis="n",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        ),
        structural=StructuralCheckReport(idea_id=idea_id, premise_evidence_ok=True,
                                         novel_hypothesis_testable=True),
        falsifiability=FalsifiabilityReport(idea_id=idea_id, testable_implication="t",
                                            unobservable_variables=[], is_falsifiable=True),
        decision=_decision(GateVerdict.PASS, None),
    )


@dataclass(frozen=True)
class _Ctx:
    tag: str


class _S(TypedDict):
    seen: Annotated[list[str], operator.add]


class LangGraphContractTest(unittest.IsolatedAsyncioTestCase):
    """钉住本设计依赖的四条 langgraph 行为。"""

    async def test_reducer_accumulates_across_fanout(self) -> None:
        # Send fan-out 的结果靠 reducer 合并；本设计据此收集候选结果
        async def fan(state: _S):
            return [Send("leaf", {"seen": [f"c{i}"]}) for i in range(3)]

        async def leaf(state: _S) -> dict:
            return {"seen": state["seen"]}

        g = StateGraph(_S)
        g.add_node("leaf", leaf)
        g.add_conditional_edges(START, fan, ["leaf"])
        g.add_edge("leaf", END)
        out = await g.compile().ainvoke({"seen": []})
        self.assertEqual({"c0", "c1", "c2"}, set(out["seen"]))

    async def test_context_carries_non_serializable_deps(self) -> None:
        # Deps 走 context_schema，不进 State——这是 checkpoint 不会碰到 Semaphore 的原因
        async def node(state: _S) -> dict:
            return {"seen": [get_runtime(_Ctx).context.tag]}

        g = StateGraph(_S, context_schema=_Ctx)
        g.add_node("node", node)
        g.add_edge(START, "node")
        g.add_edge("node", END)
        out = await g.compile().ainvoke({"seen": []}, context=_Ctx(tag="from-context"))
        self.assertEqual(["from-context"], out["seen"])

    async def test_conditional_edge_can_form_a_cycle(self) -> None:
        # 辩论循环靠这个：条件边回到自己，直到谓词说停
        class _C(TypedDict):
            n: int

        async def bump(state: _C) -> dict:
            return {"n": state["n"] + 1}

        def again(state: _C) -> str:
            return "bump" if state["n"] < 3 else END

        g = StateGraph(_C)
        g.add_node("bump", bump)
        g.add_edge(START, "bump")
        g.add_conditional_edges("bump", again, ["bump", END])
        out = await g.compile().ainvoke({"n": 0})
        self.assertEqual(3, out["n"])

    async def test_node_retry_policy_reinvokes_the_node(self) -> None:
        # 节点级重试必须在测试里可观测——设计 §5.5 选 RetryPolicy 而非纯靠传输层的理由。
        #
        # retry_on 必须显式传——langgraph 的 default_retry_on 把 RuntimeError/ValueError/
        # TypeError 等一整类异常当成"大概率是 bug，不是瞬时故障"而排除在重试之外（实测于
        # langgraph 1.2.10，见 langgraph._internal._retry.default_retry_on）。而这条流水线
        # 挂 RetryPolicy 的节点包着 LLM 调用，provider/pydantic-ai 的报错形态不定——这正是
        # review_board.py/revision.py 里旧的手搓循环一律 `except Exception` 的原因（不按类型
        # 挑）。用默认 retry_on 会让 Task 5 的节点重试对这整类异常静默失效。
        from langgraph.types import RetryPolicy

        calls = {"n": 0}

        async def flaky(state: _S) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return {"seen": ["ok"]}

        g = StateGraph(_S)
        g.add_node("flaky", flaky,
                   retry_policy=RetryPolicy(max_attempts=2, initial_interval=0.0, jitter=False,
                                             retry_on=lambda exc: True))
        g.add_edge(START, "flaky")
        g.add_edge("flaky", END)
        out = await g.compile().ainvoke({"seen": []})
        self.assertEqual(2, calls["n"])
        self.assertEqual(["ok"], out["seen"])

    async def test_multi_mode_astream_yields_mode_tagged_chunks(self) -> None:
        # 事件桥接依赖：updates 给节点名，values 给最终状态，两者要能同时拿到
        class _C(TypedDict):
            n: int

        g = StateGraph(_C)
        g.add_node("a", lambda s: {"n": 1})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        seen = []
        async for chunk in g.compile().astream({"n": 0}, stream_mode=["updates", "values"]):
            seen.append(chunk)
        modes = {c[0] for c in seen if isinstance(c, tuple)}
        self.assertEqual({"updates", "values"}, modes)


class StateDepsSplitTest(unittest.TestCase):
    def test_deps_holds_only_runtime_objects(self) -> None:
        # 拆分规则：不可序列化的东西一律在 Deps，不进 State
        from athena.workflows.search.state import PipelineDeps
        for field in ("artifacts", "corpus_ref", "llm_sem", "retrieval_sem",
                      "gap_miner_agent", "novelty_agent", "domain_review_agent", "model"):
            self.assertIn(field, PipelineDeps.__dataclass_fields__)

    def test_state_carries_no_runtime_objects(self) -> None:
        # 反向：这些名字绝不能出现在 State 里，否则 checkpoint 会撞上不可序列化对象
        from athena.workflows.search.state import CandidateState, PipelineState
        forbidden = {"artifacts", "llm_sem", "retrieval_sem",
                     "gap_miner_agent", "novelty_agent", "domain_review_agent", "model"}
        self.assertEqual(set(), forbidden & set(PipelineState.__annotations__))
        self.assertEqual(set(), forbidden & set(CandidateState.__annotations__))

    def test_candidate_state_carries_index_for_ordering(self) -> None:
        # 设计 §4.4：Send fan-out 后靠显式 index 恢复候选输入序
        from athena.workflows.search.state import CandidateState
        self.assertIn("index", CandidateState.__annotations__)

    def test_deps_is_frozen(self) -> None:
        from athena.workflows.search.state import PipelineDeps
        self.assertTrue(PipelineDeps.__dataclass_params__.frozen)


class PipelineShellTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_full_pipeline_keeps_its_signature_and_return_shape(self) -> None:
        # 迁移策略的支点：签名与返回值不变，18 处既有端到端调用因此继续充当回归网。
        #
        # 只断言参数名"在场"（旧版本）测不出新增一个必填参数的回归——新增的必填参数同样会
        # "在场"，membership 检查看不出区别，而这恰恰是会砸掉全部 18 处既有调用点的变化。
        #
        # 这里没有采用"除 problem 外都必须有默认值"这条规则：run_full_pipeline 现有签名里
        # gap_miner_agent/novelty_agent/domain_review_agent/artifacts/corpus_ref 本来就是
        # 无默认值的必填关键字参数（Agent/ArtifactStore/ArtifactRef 类型的运行时依赖，没有
        # 合理的默认值可给），不是需要修的回归。真正该钉住的是"必填参数集合"与"可选参数集合"
        # 这两个集合本身的构成——精确到集合相等：新增一个必填参数会让 required 集合多出一个
        # 未预期的名字，某个既有可选参数被改成必填、或反过来，也会被两个集合的相等断言抓到。
        import inspect

        from athena.workflows.search.workflow import run_full_pipeline

        params = inspect.signature(run_full_pipeline).parameters
        required = {name for name, p in params.items() if p.default is inspect.Parameter.empty}
        optional = {name for name, p in params.items() if p.default is not inspect.Parameter.empty}

        self.assertEqual(
            {"problem", "gap_miner_agent", "novelty_agent", "domain_review_agent",
             "artifacts", "corpus_ref"},
            required,
        )
        self.assertEqual(
            {"sample_size", "model", "thread_id", "checkpointer", "emit"},
            optional,
        )

    async def test_emit_reaches_the_candidate_subgraph_through_run_full_pipeline(self) -> None:
        # I4：事件桥接不能只在 run_graph 上可用。run_full_pipeline 是文档声明的唯一入口，
        # 它组装 PipelineDeps 这件事本身就是调用方使用它的理由——emit 传不进来的话，想拿
        # 候选级事件就必须绕过它自己拼 PipelineDeps，"唯一入口"这条约束当场失效。
        # 断言取候选级事件（带 candidate_index 的那种）而不是顶层事件：顶层事件只证明
        # emit 到了 run_graph，候选级事件才证明它经 deps 一路带进了候选子图。
        import athena.workflows.search.graph as graph_mod
        from athena.workflows.search.workflow import run_full_pipeline

        async def fake_mine(*a, **kw):
            return []

        async def fake_generate(*a, **kw):
            return [HypothesisPackage(
                idea_id="idea-1", generation_strategy="s", novel_hypothesis="h",
                supported_premises=[], inference_chain=[], predicted_observations=["p"],
                disconfirming_observations=["d"], lineage_op="generate",
            )]

        async def fake_falsifiability_check(*a, **kw):
            # 强制降级 -> pre_gate 判 REVISE -> screen 后直接 END，不必搭 novelty/审阅的替身
            raise RuntimeError("forced degrade")

        for name, fake in (("mine_research_gaps", fake_mine),
                           ("generate_candidates", fake_generate),
                           ("falsifiability_check", fake_falsifiability_check)):
            self.addCleanup(setattr, graph_mod, name, getattr(graph_mod, name))
            setattr(graph_mod, name, fake)

        events = []

        async def emit(kind, ref, data=None):
            events.append((kind, ref, data))

        with tempfile.TemporaryDirectory() as tmp:
            from athena.storage.artifact_store import LocalArtifactStore

            deps = _deps()
            await run_full_pipeline(
                _problem(), gap_miner_agent=deps.gap_miner_agent,
                novelty_agent=deps.novelty_agent, domain_review_agent=deps.domain_review_agent,
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=1, emit=emit,
            )

        candidate_steps = [data for kind, _ref, data in events
                           if kind == "idea_generation/step" and "candidate_index" in (data or {})]
        self.assertTrue(candidate_steps, "no candidate-level events reached the emit callback")
        self.assertEqual({0}, {data["candidate_index"] for data in candidate_steps})

    async def test_graph_has_the_top_level_nodes(self) -> None:
        from athena.workflows.search.graph import build_pipeline_graph

        nodes = set(build_pipeline_graph().compile().get_graph().nodes)
        for name in ("gap_mining", "generate", "collect", "rank"):
            self.assertIn(name, nodes)


class CollectNodeOrderingTest(unittest.IsolatedAsyncioTestCase):
    """直接测 collect_node 自己的排序逻辑，不经过 RevisionLoopConcurrencyTest 那条依赖真实
    并发调度时序的端到端路径——Task 8 变异 1 发现 Elo 确定性测试在 FunctionModel 驱动下
    Send 分支恒按提交序完成，对"保序"这件事没有区分力（同一次跑两次结果一致，但不代表真的
    按 index 排过序）。这里用手工构造的乱序 results 元组把 collect_node 的排序逻辑单独钉住，
    不依赖任何调度时序。"""

    async def test_collect_node_sorts_by_index_not_arrival_order(self) -> None:
        # 三个候选故意按 index 逆序放进 results，模拟 Send 分支按 2 -> 0 -> 1 完成到达：
        # 如果 collect_node 直接按到达序拼接（丢了 sorted），ordered_results 会是
        # [r2, r0, r1]，断言会失败。
        r0, r1, r2 = _pipeline_result("idea-0"), _pipeline_result("idea-1"), _pipeline_result("idea-2")
        from athena.workflows.search.graph import collect_node

        state = {"results": [(2, r2), (0, r0), (1, r1)]}
        patch_result = await collect_node(state)

        self.assertEqual([r0, r1, r2], patch_result["ordered_results"])


class RankNodeConsumesOrderedResultsTest(unittest.IsolatedAsyncioTestCase):
    """CollectNodeOrderingTest 只证明了 collect **排了序**，没有任何测试证明 rank **消费的是
    排好序的那个字段**——把 rank_node 的 `state["ordered_results"]` 换成直接读 `results`
    累加器，全量 560 条测试依旧全绿（Task 9 变异 M9 实测）。原因与上一轮 Elo 确定性测试
    失去区分力是同一个：FunctionModel 驱动下 Send 分支恒按提交序完成，`results` 的到达序
    恰好等于 index 序，两个字段在测试里恒等。

    这里绕开调度时序，直接构造两份状态：两份的 `ordered_results` 都是有序的，只有 `results`
    累加器的顺序不同（一份模拟乱序到达）。读 ordered_results 的实现两次结果必然相同；读
    results 的实现会因为 Elo 是在线增量更新而给出不同评分。
    """

    @staticmethod
    async def _fake_pairwise(package_a, package_b, *, llm_sem=None, model=None):
        """确定性比较器：idea_id 字典序小的恒胜。胜负与喂入顺序无关，所以两次运行的评分
        若不同，只可能来自 Elo 的喂入顺序，不会来自判决本身。"""
        return PairwiseComparison(
            idea_id_a=package_a.idea_id, idea_id_b=package_b.idea_id,
            winner_id=min(package_a.idea_id, package_b.idea_id), rationale="lowest id wins",
        )

    async def _rank(self, results_order: list[int], ordered_order: list[int]) -> list[tuple]:
        """把 rank_node 挂进一张只有它自己的图跑一次——rank_node 用 get_runtime 取依赖，
        必须在 langgraph 的 runtime context 里执行，不能直接 await 它。"""
        import athena.workflows.search.graph as graph_mod

        by_index = {i: _pipeline_result(f"idea-{i}") for i in range(4)}

        # 状态 schema 直接用 PipelineState，不另写一个精简版：rank_node 的入参注解就是
        # PipelineState，langgraph 会把注解里的 channel 一并注册进图，自定义 schema 若把
        # results 声明成没有 reducer 的普通字段，两处会撞成
        # "Channel 'results' already exists with a different type"。
        graph = StateGraph(PipelineState, context_schema=PipelineDeps)
        graph.add_node("rank", graph_mod.rank_node)
        graph.add_edge(START, "rank")
        graph.add_edge("rank", END)

        original = graph_mod.pairwise_compare
        graph_mod.pairwise_compare = self._fake_pairwise
        try:
            final = await graph.compile().ainvoke(
                {"results": [(i, by_index[i]) for i in results_order],
                 "ordered_results": [by_index[i] for i in ordered_order]},
                context=_deps(),
            )
        finally:
            graph_mod.pairwise_compare = original
        return [(entry.idea_id, entry.rating) for entry in final["ranking"]]

    async def test_ranking_ignores_the_results_accumulator_arrival_order(self) -> None:
        # 两次的 ordered_results 完全相同；只有 results 累加器的顺序不同（第二次模拟
        # Send 分支按 2 -> 0 -> 3 -> 1 完成到达）。rank_node 读 ordered_results 时两次
        # 评分必须逐位相等；读 results 时会得到不同评分（已实测四个候选足以拉开差异，
        # 两个候选只有一次比较，测不出喂入顺序这回事）。
        in_order = await self._rank(results_order=[0, 1, 2, 3], ordered_order=[0, 1, 2, 3])
        scrambled = await self._rank(results_order=[2, 0, 3, 1], ordered_order=[0, 1, 2, 3])
        self.assertEqual(in_order, scrambled)


class CandidateSubgraphTest(unittest.IsolatedAsyncioTestCase):
    def test_subgraph_has_one_node_per_review_perspective(self) -> None:
        from athena.workflows.search.graph import build_candidate_graph
        from athena.workflows.search.review_board import REVIEW_PERSPECTIVES

        nodes = set(build_candidate_graph().compile().get_graph().nodes)
        for perspective in REVIEW_PERSPECTIVES:
            self.assertIn(f"review_{perspective.perspective_id}", nodes)

    def test_screen_failure_skips_the_expensive_nodes(self) -> None:
        # pre_gate 判 REVISE 的候选不得进入 novelty/审阅——那是 [5]-[8] 的昂贵段
        from athena.workflows.search.graph import route_after_screen

        revise = _decision(GateVerdict.REVISE, "falsifiable")
        self.assertEqual(END, route_after_screen({"decision": revise}))
        self.assertEqual("novelty", route_after_screen({"decision": _decision(GateVerdict.PASS, None)}))


class DebateCycleTest(unittest.IsolatedAsyncioTestCase):
    def test_no_op_revision_leaves_the_cycle_without_refresh(self) -> None:
        # 设计 §3.3：refresh/regate 只在真的产生了修订时才在路径上——图上根本没有那条边，
        # 而不是靠一个 revised 布尔变量记得判断
        from athena.workflows.search.graph import route_after_revise

        self.assertEqual(END, route_after_revise({"package": _pkg(), "revision_round_before": 0}))

    def test_cycle_stops_at_max_debate_rounds(self) -> None:
        from athena.workflows.search.graph import route_after_rereview
        from athena.workflows.search.revision import MAX_DEBATE_ROUNDS

        state = {"revisions": [_round(i + 1) for i in range(MAX_DEBATE_ROUNDS)], "cleared": False}
        self.assertEqual("refresh", route_after_rereview(state))

    def test_cycle_continues_when_not_cleared_and_rounds_remain(self) -> None:
        from athena.workflows.search.graph import route_after_rereview

        self.assertEqual("revise", route_after_rereview({"revisions": [_round(1)], "cleared": False}))

    def test_pending_draft_signals_a_fresh_round_regardless_of_revision_round(self) -> None:
        # route_after_revise 的问题 B：pending_draft 非 None 说明这一轮真的产出了新草稿，
        # 直接去 rereview，不看 revision_round_before——即使 package.revision_round 恰好没有
        # 比 revision_round_before 更大（这里故意都传 0），这条判断也不该影响 rereview 分支。
        from athena.workflows.search.graph import route_after_revise
        from athena.workflows.search.idea_schemas import RevisionDraft

        draft = RevisionDraft(
            rebuttal="r", changes_made=["c"], revised_novel_hypothesis="h",
            revised_premises=[], revised_predicted_observations=["p"],
            revised_disconfirming_observations=["d"],
        )
        state = {"package": _pkg(), "pending_draft": draft, "revision_round_before": 0}
        self.assertEqual("rereview", route_after_revise(state))

    def test_no_pending_draft_but_an_earlier_round_advanced_routes_to_refresh(self) -> None:
        # route_after_revise 的问题 A：这一轮没有产出新草稿（pending_draft 为 None，比如
        # 第 2 轮 reviser 失败或 no-op），但 package.revision_round 比进入辩论前更大（第 1
        # 轮确实推进过）——该去 refresh 而不是 END。这条正是 C1 finding 里"两个独立问题不能
        # 共用同一个信号"的核心：只测 revision_round_before 是否只缓存一次不够，必须同时验证
        # pending_draft 缺失时仍然走 refresh 而非误判成 rereview。
        from athena.workflows.search.graph import route_after_revise

        advanced_pkg = _pkg().model_copy(update={"revision_round": 1})
        state = {"package": advanced_pkg, "revision_round_before": 0}
        self.assertEqual("refresh", route_after_revise(state))

    def test_rereview_failure_routes_to_refresh_not_revise(self) -> None:
        # C2：对手重表态失败与 run_debate 的 try/except: break 等价，必须立即结束循环——
        # 即使轮次未满（这里只有 1 轮记录，MAX_DEBATE_ROUNDS=2）也不能像 cleared=False 那样
        # 回 revise 再打一轮。
        from athena.workflows.search.graph import route_after_rereview

        state = {"rereview_failed": True, "cleared": False, "revisions": [_round(1)]}
        self.assertEqual("refresh", route_after_rereview(state))


class MultiRoundDebateGraphIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """驱动 run_full_pipeline（编排已完全交给 graph.py 的 langgraph 图）跑一次真正的 2 轮
    辩论，专门覆盖 C1/C2 两个 Critical 缺陷——七轮任务级 review 都没抓住的原因是：没有任何
    既有测试真的让第 2 轮 revise_node 走"失败/no-op"或"对手重表态失败"这两条收尾分支，全部
    走的是"两轮都真实修订"或"第一轮就收敛/不收敛到 MAX_DEBATE_ROUNDS"这类路径，恰好绕开了
    revision_round_before 缓存时机与 rereview_failed 信号这两处 bug。

    两条用例都直接跑 run_full_pipeline 而不是手工拼 CandidateState 调
    build_candidate_graph()：候选子图从 START 起步必经 screen/novelty/review 三个真实节点
    （structural_check/falsifiability_check/collect_novelty_evidence 等），绕不过去，手工
    拼状态并不能真的省掉这些依赖的搭建成本，反而会跳过图的真实入口；直接复用
    test_workflow.py::RevisionLoopPipelineTest 已经验证过的 run_full_pipeline + 路由假模型
    这套测试基础设施更直接。
    """

    async def test_round1_succeeds_not_cleared_round2_no_ops_reaches_refresh(self) -> None:
        # C1：第 1 轮 reviser 真实修订成功、对手没清除；第 2 轮 reviser 产出逐字段不变的
        # no-op 草稿。旧实现里 revision_round_before 每轮都重新取当前 package.revision_round
        # （进入第 2 轮时已经是 1），第 2 轮 no-op 不推进 package，"1 > 1" 为假 -> 错误地路由
        # 到 END，refresh/regate 整段被跳过，revision_blocking_factor 恒为 None。只缓存一次
        # revision_round_before、但不引入 pending_draft 信号的话，"package.revision_round(1)
        # > revision_round_before(缓存的 0)"在这个场景里反而为真，会误路由到 rereview，拿
        # 第 1 轮的旧 pending_draft 再给对手打一次电话——两处子缺陷都会在这个场景里现形。
        import athena.workflows.search.graph as graph_mod
        from pydantic_ai.models.function import FunctionModel

        from athena.storage import LocalArtifactStore
        from athena.workflows.search.gatekeeper import MAX_TOLERATED_RISKS
        from athena.workflows.search.idea_schemas import (
            GapMiningResponse, RevisionDraft, SkepticJudgment,
        )
        from athena.workflows.search.workflow import run_full_pipeline
        from unit.fakes import tool_call_response
        from unit.test_workflow import (
            _ROUTE_FALSIFIABILITY, _ROUTE_GAP_MINING, _ROUTE_GENERATION, _ROUTE_NOVELTY,
            _ROUTE_REVIEW_DOMAIN, _ROUTE_REVIEW_METHODOLOGY, _ROUTE_REVIEW_STATISTICS,
            _build_retrieval_agent, _clean_novelty_judgment, _falsifiability_judgment,
            _problem, _valid_draft,
        )

        draft = _valid_draft()
        blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
        round1_disconfirming = [
            "Y stays the same after X knockout, round 1 revision (matched control cohort)"]
        round1_draft = RevisionDraft(
            rebuttal="a matched control cohort now grounds the disconfirmer",
            changes_made=["tightened the disconfirming observation"],
            revised_novel_hypothesis=draft.statement,
            revised_premises=draft.supported_premises,
            revised_predicted_observations=draft.predicted_observations,
            revised_disconfirming_observations=round1_disconfirming,
        )
        round1_critique = "still underpowered after revision"
        # 第 2 轮 no-op：逐字段照抄第 1 轮修订后的 package——is_no_op_revision 比对的是
        # revise_candidate 被调用时那份 package，也就是第 1 轮修订后的稿子，不是最初的草稿。
        round2_no_op_draft = RevisionDraft(
            rebuttal="nothing further to add",
            changes_made=[],
            revised_novel_hypothesis=draft.statement,
            revised_premises=draft.supported_premises,
            revised_predicted_observations=draft.predicted_observations,
            revised_disconfirming_observations=round1_disconfirming,
        )

        fixed_routes = {
            _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
            _ROUTE_GENERATION: draft,
            _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
            _ROUTE_NOVELTY: _clean_novelty_judgment(),
            _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
            _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                critique="underpowered", unaddressed_risks=blocking_risks, fatal_flaw_found=False),
            _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        }
        # reviser 两轮各自的锚点：第 1 轮 prior_rounds 为空 -> prompt 里含
        # "(this is the first round)"；第 2 轮 prior_rounds=[第 1 轮摘要] -> prompt 里含第 1
        # 轮摘要拼出的 "reviewer replied=<critique>"（照抄
        # test_workflow.py::RevisionLoopPipelineTest._run_non_converging 的技巧）。
        reviser_routes = {
            "(this is the first round)": round1_draft,
            f"reviewer replied={round1_critique}": round2_no_op_draft,
        }
        debate_response = SkepticJudgment(
            critique=round1_critique, unaddressed_risks=blocking_risks, fatal_flaw_found=False)

        rereview_calls = {"n": 0}

        def respond(messages, info):
            prompt = str(messages)
            if "Debate round" in prompt:
                rereview_calls["n"] += 1
                return tool_call_response(debate_response, info)
            for key, value in reviser_routes.items():
                if key in prompt:
                    return tool_call_response(value, info)
            hits = [key for key in fixed_routes if key in prompt]
            assert len(hits) == 1, (hits, prompt[:200])
            return tool_call_response(fixed_routes[hits[0]], info)

        original_revise_candidate = graph_mod.revise_candidate
        revise_calls = {"n": 0}

        async def counting_revise_candidate(*args, **kwargs):
            # 直接数 graph.py 里 revise_node 调用 revise_candidate 的次数——旧 bug 下第 2
            # 轮结束后会被误路由回 rereview 再拿旧草稿多打一次电话，但那不经过
            # revise_candidate 本身；这里计数的是 reviser 侧的调用次数，用来确认"确实跑了
            # 恰好 2 轮 revise_node"，配合下面的 rereview_calls 一起把 (c) 钉死。
            revise_calls["n"] += 1
            return await original_revise_candidate(*args, **kwargs)

        graph_mod.revise_candidate = counting_revise_candidate
        try:
            with tempfile.TemporaryDirectory() as tmp:
                artifacts = LocalArtifactStore(tmp)
                results, _ranking = await run_full_pipeline(
                    _problem(), gap_miner_agent=_build_retrieval_agent(),
                    novelty_agent=_build_retrieval_agent(),
                    domain_review_agent=_build_retrieval_agent(),
                    artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1,
                    model=FunctionModel(respond),
                )
        finally:
            graph_mod.revise_candidate = original_revise_candidate

        result = results[0]
        # (a)+(b)：revision_blocking_factor 只有 regate_node 才会写，非 None 就证明
        # refresh -> regate 真的跑完了，且值必须是辩论前 gate_node 那次的 blocking_factor
        # （旧 bug 下第 2 轮 no-op 会误路由到 END，这个字段恒为 None）。
        self.assertEqual("risk_ok_statistics", result.revision_blocking_factor)
        # (c)：reviser 恰好被调用 2 次（第 1 轮真实修订 + 第 2 轮 no-op 尝试），对手重表态
        # 恰好被调用 1 次（只有第 1 轮）——旧 bug 下会拿第 1 轮的旧 pending_draft 对第 2 轮
        # 再多打一次电话给对手，这里会变成 2。
        self.assertEqual(2, revise_calls["n"])
        self.assertEqual(1, rereview_calls["n"])
        # 辩论记录本身也要留痕：1 轮真实修订 + 1 轮 no-op，两条都未清除
        self.assertEqual(2, len(result.revisions))
        self.assertFalse(result.revisions[0].cleared)
        self.assertFalse(result.revisions[1].cleared)
        self.assertIsNone(result.revisions[1].reviewer_response_ref)

    async def test_round1_opponent_rereview_fails_ends_debate_without_a_spurious_second_round(
        self,
    ) -> None:
        # C2：第 1 轮 reviser 真实修订成功、随即对手重表态本身失败（single_turn_chat 抛错）。
        # 这个失败必须发生在**第 1 轮**而不是第 2 轮（最后一轮）：MAX_DEBATE_ROUNDS=2，若失败
        # 发生在第 2 轮，len(revisions)>=MAX_DEBATE_ROUNDS 这条件本身就已经为真，旧 bug（漏了
        # rereview_failed 信号）和新代码在那个场景下会殊途同归地都走到 refresh，测不出区别；
        # 只有在还有"剩余轮次"的第 1 轮失败，才能让旧 bug（cleared=False 与"轮次未满"叠加）
        # 真正暴露出"多打一轮"的效果。
        #
        # run_debate 的等价行为是 try/except: break——package 已经推进到第 1 轮修订稿，但辩论
        # 到此立即结束，不该再回 revise 打第 2 轮。旧实现下 route_after_rereview 只看
        # cleared=False 和轮次是否已满，第 1 轮失败时两个条件都不满足"该停"，于是错误地回
        # revise：finding 原文强调的"reviser 提示词在这次多余的下一轮里错误地宣称
        # '(this is the first round)'"，是因为失败的这轮从未往 prior_summaries 里追加内容——
        # 这里复用同一个 "(this is the first round)" 锚点给"多余的第 2 轮"提供响应，既贴合
        # finding 描述的真实症状，也让 is_no_op_revision 判定这次多余调用为 no-op（用同一份
        # 草稿去修一份已经改过的 package，逐字段相等），所以能观测到的区别集中在调用计数与
        # revisions 条数上，不在于最终是否到达 refresh（no-op 分支本身也会级联到 refresh）。
        import athena.workflows.search.graph as graph_mod
        from pydantic_ai.models.function import FunctionModel

        from athena.storage import LocalArtifactStore
        from athena.workflows.search.gatekeeper import MAX_TOLERATED_RISKS
        from athena.workflows.search.idea_schemas import (
            GapMiningResponse, RevisionDraft, SkepticJudgment,
        )
        from athena.workflows.search.workflow import run_full_pipeline
        from unit.fakes import tool_call_response
        from unit.test_workflow import (
            _ROUTE_FALSIFIABILITY, _ROUTE_GAP_MINING, _ROUTE_GENERATION, _ROUTE_NOVELTY,
            _ROUTE_REVIEW_DOMAIN, _ROUTE_REVIEW_METHODOLOGY, _ROUTE_REVIEW_STATISTICS,
            _build_retrieval_agent, _clean_novelty_judgment, _falsifiability_judgment,
            _problem, _valid_draft,
        )

        draft = _valid_draft()
        blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
        round1_disconfirming = [
            "Y stays the same after X knockout, round 1 revision (matched control cohort)"]
        round1_draft = RevisionDraft(
            rebuttal="a matched control cohort now grounds the disconfirmer",
            changes_made=["tightened the disconfirming observation"],
            revised_novel_hypothesis=draft.statement,
            revised_premises=draft.supported_premises,
            revised_predicted_observations=draft.predicted_observations,
            revised_disconfirming_observations=round1_disconfirming,
        )

        fixed_routes = {
            _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
            _ROUTE_GENERATION: draft,
            _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
            _ROUTE_NOVELTY: _clean_novelty_judgment(),
            _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
            _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                critique="underpowered", unaddressed_risks=blocking_risks, fatal_flaw_found=False),
            _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        }

        rereview_calls = {"n": 0}

        def respond(messages, info):
            prompt = str(messages)
            if "Debate round" in prompt:
                # 对手重表态每次都失败——第 1 轮就该让辩论到此为止，绝不该被第二次调用到
                rereview_calls["n"] += 1
                raise RuntimeError("opponent provider down")
            if "(this is the first round)" in prompt:
                # 失败的第 1 轮没有 prior_summaries 可加，如果 bug 触发多余的第 2 轮，reviser
                # prompt 依旧会命中这个锚点——同一份 round1_draft 拿去修一份已经是这份草稿的
                # package，逐字段相等，is_no_op_revision 判定为 no-op
                return tool_call_response(round1_draft, info)
            hits = [key for key in fixed_routes if key in prompt]
            assert len(hits) == 1, (hits, prompt[:200])
            return tool_call_response(fixed_routes[hits[0]], info)

        original_revise_candidate = graph_mod.revise_candidate
        revise_calls = {"n": 0}

        async def counting_revise_candidate(*args, **kwargs):
            revise_calls["n"] += 1
            return await original_revise_candidate(*args, **kwargs)

        graph_mod.revise_candidate = counting_revise_candidate
        try:
            with tempfile.TemporaryDirectory() as tmp:
                artifacts = LocalArtifactStore(tmp)
                results, _ranking = await run_full_pipeline(
                    _problem(), gap_miner_agent=_build_retrieval_agent(),
                    novelty_agent=_build_retrieval_agent(),
                    domain_review_agent=_build_retrieval_agent(),
                    artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1,
                    model=FunctionModel(respond),
                )
        finally:
            graph_mod.revise_candidate = original_revise_candidate

        result = results[0]
        # 核心区分信号：reviser 只该被调用 1 次（旧 bug 下会因为误路由回 revise 而多打一轮，
        # 变成 2 次），对手重表态同样只该被调用 1 次（成功与否无所谓，只要求恰好被联系一次，
        # 不会因为失败就被联系第二次）。
        self.assertEqual(1, revise_calls["n"])
        self.assertEqual(1, rereview_calls["n"])
        # 辩论记录只留 1 条（第 1 轮，失败、未清除）；旧 bug 下会多出第 2 轮那条 no-op 记录
        self.assertEqual(1, len(result.revisions))
        self.assertFalse(result.revisions[0].cleared)
        self.assertIsNone(result.revisions[0].reviewer_response_ref)
        # refresh/regate 仍然跑完：revision_blocking_factor 非 None，且是辩论前的
        # blocking_factor——失败即停不等于"辩论从未推进过"，第 1 轮的修订已经落地
        self.assertEqual("risk_ok_statistics", result.revision_blocking_factor)


    async def test_round2_reviser_failure_does_not_reuse_round1s_stale_draft(self) -> None:
        # I1：revise_node 的 reviser-失败分支显式把 pending_draft 清成 None（graph.py 该分支
        # 里那行注释说的就是这条）。删掉那一行，全量 560 条测试依旧全绿——no-op 分支的同一个
        # 清空动作有覆盖（上一条用例），失败分支的没有。
        #
        # 场景必须是**第 2 轮** reviser 失败，且第 1 轮真的推进过：第 1 轮就失败时
        # pending_draft 从来没被写过，残不残留没有区别，测不出这行代码。第 1 轮真实修订 +
        # 对手回应但未清除，才会在进入第 2 轮时把一份非 None 的旧草稿留在 state 里；第 2 轮
        # reviser 抛错后若不清空，route_after_revise 会把它当成"本轮产出了新草稿"，拿第 1 轮
        # 的旧草稿再给对手打一次电话（多一次 LLM 调用 + 多一条语义错误的 RevisionRound），
        # 与 run_debate 的 try/except: break 直接冲突。
        import athena.workflows.search.graph as graph_mod
        from pydantic_ai.models.function import FunctionModel

        from athena.storage import LocalArtifactStore
        from athena.workflows.search.gatekeeper import MAX_TOLERATED_RISKS
        from athena.workflows.search.idea_schemas import (
            GapMiningResponse, RevisionDraft, SkepticJudgment,
        )
        from athena.workflows.search.workflow import run_full_pipeline
        from unit.fakes import tool_call_response
        from unit.test_workflow import (
            _ROUTE_FALSIFIABILITY, _ROUTE_GAP_MINING, _ROUTE_GENERATION, _ROUTE_NOVELTY,
            _ROUTE_REVIEW_DOMAIN, _ROUTE_REVIEW_METHODOLOGY, _ROUTE_REVIEW_STATISTICS,
            _build_retrieval_agent, _clean_novelty_judgment, _falsifiability_judgment,
            _problem, _valid_draft,
        )

        draft = _valid_draft()
        blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
        round1_draft = RevisionDraft(
            rebuttal="a matched control cohort now grounds the disconfirmer",
            changes_made=["tightened the disconfirming observation"],
            revised_novel_hypothesis=draft.statement,
            revised_premises=draft.supported_premises,
            revised_predicted_observations=draft.predicted_observations,
            revised_disconfirming_observations=[
                "Y stays the same after X knockout, round 1 revision (matched control cohort)"],
        )
        round1_critique = "still underpowered after revision"

        fixed_routes = {
            _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
            _ROUTE_GENERATION: draft,
            _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
            _ROUTE_NOVELTY: _clean_novelty_judgment(),
            _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
            _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                critique="underpowered", unaddressed_risks=blocking_risks, fatal_flaw_found=False),
            _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        }
        # 对手每轮都回应但从不清除风险，第 1 轮因此不会提前结束辩论
        debate_response = SkepticJudgment(
            critique=round1_critique, unaddressed_risks=blocking_risks, fatal_flaw_found=False)

        rereview_calls = {"n": 0}
        revise_calls = {"n": 0}

        def respond(messages, info):
            prompt = str(messages)
            if "Debate round" in prompt:
                rereview_calls["n"] += 1
                return tool_call_response(debate_response, info)
            if "(this is the first round)" in prompt:
                revise_calls["n"] += 1
                return tool_call_response(round1_draft, info)
            if f"reviewer replied={round1_critique}" in prompt:
                # 第 2 轮 reviser 直接抛错（revise_candidate 单次调用即向外抛，不重试）
                revise_calls["n"] += 1
                raise RuntimeError("reviser provider down")
            hits = [key for key in fixed_routes if key in prompt]
            assert len(hits) == 1, (hits, prompt[:200])
            return tool_call_response(fixed_routes[hits[0]], info)

        with tempfile.TemporaryDirectory() as tmp:
            results, _ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=1, model=FunctionModel(respond),
            )

        result = results[0]
        # reviser 两轮各一次（第 2 轮那次抛错）；对手只在第 1 轮被联系过一次——残留旧草稿
        # 会让这个数变成 2。
        self.assertEqual(2, revise_calls["n"])
        self.assertEqual(1, rereview_calls["n"])
        # 只留第 1 轮那条记录；残留旧草稿会多出一条用旧草稿伪造的第 2 轮记录
        self.assertEqual(1, len(result.revisions))
        self.assertEqual(1, result.revisions[0].round_index)
        # 第 1 轮的修订确实落地了，所以 refresh -> regate 仍然跑完（与 run_debate 一致：
        # 失败即停不等于"从未推进过"）
        self.assertEqual("risk_ok_statistics", result.revision_blocking_factor)


class RetryRelocationTest(unittest.IsolatedAsyncioTestCase):
    def test_revision_draft_enforces_the_falsifiability_invariant(self) -> None:
        # 把不变量上提到 LLM 面向的 schema，pydantic-ai 的输出校验重试原生接管；
        # 校验器本身不放宽——它是 evidence_traceable 这项 rubric 的结构基础
        from pydantic import ValidationError

        from athena.workflows.search.idea_schemas import RevisionDraft

        with self.assertRaises(ValidationError):
            RevisionDraft(rebuttal="r", changes_made=[], revised_novel_hypothesis="h",
                          revised_premises=[], revised_predicted_observations=["p"],
                          revised_disconfirming_observations=[])

    def test_no_hand_rolled_retry_loops_remain_in_the_graph_path(self) -> None:
        # 两个手搓循环删除后，模块里不该再有它们的常量
        import athena.workflows.search.review_board as rb
        import athena.workflows.search.revision as rv

        self.assertFalse(hasattr(rb, "MAX_REVIEW_ATTEMPTS"))
        self.assertFalse(hasattr(rv, "MAX_REVISION_ATTEMPTS"))


class CheckpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_run_resumes_from_the_checkpoint(self) -> None:
        # 让 generate 第一次抛、第二次成功，用同一个 thread_id 续跑，断言已完成的
        # gap_mining 没有重跑（调用计数不变），而失败过的 generate 确实重跑了一次
        # （调用计数从 1 变 2）——这是 checkpoint/续跑机制本身，不是业务逻辑，
        # 用最简单能触发 gap_mining -> generate 两步顺序边的路径验证即可，不需要
        # 真的构造候选、走完候选子图
        import athena.workflows.search.graph as graph_mod
        from langgraph.checkpoint.memory import InMemorySaver

        from athena.core.agent import Agent, AgentConfig, StreamEvent
        from athena.core.tool import ToolRegistry
        from athena.storage.artifact_store import LocalArtifactStore
        from athena.workflows.search.graph import run_graph
        from athena.workflows.search.idea_schemas import ResearchProblemInput
        from athena.workflows.search.state import PipelineDeps

        _FAKE_CORPUS_REF = "sha256:" + "c" * 64

        class _StaticTextProvider:
            async def stream(self, *_args):
                yield StreamEvent("text_delta", {"delta": "x", "accumulated": "x"})
                yield StreamEvent("response_completed")

        def _agent() -> Agent:
            agent = Agent(AgentConfig(model="test-model", system_prompt="s",
                                       tools=ToolRegistry()))
            agent._provider = _StaticTextProvider()
            return agent

        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            deps = PipelineDeps(
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF,
                llm_sem=asyncio.Semaphore(16), retrieval_sem=asyncio.Semaphore(4),
                gap_miner_agent=_agent(), novelty_agent=_agent(), domain_review_agent=_agent(),
                model=None,
            )
            checkpointer = InMemorySaver()
            thread_id = "checkpoint-test-thread"
            problem = ResearchProblemInput(question="q", domain="biology",
                                            objective="o", evidence_texts=["e"])

            gap_calls = {"n": 0}
            async def fake_mine(*a, **kw):
                gap_calls["n"] += 1
                return []
            gen_calls = {"n": 0}
            async def fake_generate(*a, **kw):
                gen_calls["n"] += 1
                if gen_calls["n"] == 1:
                    raise RuntimeError("boom")
                return []

            orig_mine, orig_gen = graph_mod.mine_research_gaps, graph_mod.generate_candidates
            graph_mod.mine_research_gaps = fake_mine
            graph_mod.generate_candidates = fake_generate
            try:
                with self.assertRaises(RuntimeError):
                    await run_graph(problem, deps=deps, thread_id=thread_id,
                                     checkpointer=checkpointer)

                results, ranking = await run_graph(problem, deps=deps, thread_id=thread_id,
                                                    checkpointer=checkpointer)
            finally:
                graph_mod.mine_research_gaps = orig_mine
                graph_mod.generate_candidates = orig_gen

            self.assertEqual([], results)
            self.assertEqual([], ranking)
            self.assertEqual(1, gap_calls["n"])   # 已完成，续跑没有重跑
            self.assertEqual(2, gen_calls["n"])   # 失败过，续跑重跑了一次并成功

    async def test_rerunning_a_completed_thread_raises_instead_of_duplicating_results(self) -> None:
        # I2：thread 跑完过一次之后（state.next 为空、state.values 非空），再拿同一个
        # thread_id/checkpointer 调 run_graph 必须直接抛 ValueError——而不是把非 None 的
        # input 当成"在这条 thread 上开一次新的运行"悄悄重跑一遍。旧实现只检查了
        # `state.next` 非空这一种情况（中断续跑），没检查"已跑完"这第二种情况：results 是
        # operator.add 累加 channel，同一条 thread 上的第二次运行不会把它清空，第二轮的
        # (index, result) 元组会直接叠加到第一轮的存量上，ordered_results/candidates 数量
        # 翻倍，进而 Elo pairwise 排序会把同一个候选拿去和自己的复制品比较。
        import athena.workflows.search.graph as graph_mod
        from langgraph.checkpoint.memory import InMemorySaver

        from athena.workflows.search.graph import run_graph
        from athena.workflows.search.idea_schemas import ResearchProblemInput
        from athena.workflows.search.state import PipelineDeps

        with tempfile.TemporaryDirectory() as tmp:
            from athena.core.agent import Agent, AgentConfig, StreamEvent
            from athena.core.tool import ToolRegistry
            from athena.storage.artifact_store import LocalArtifactStore

            class _StaticTextProvider:
                async def stream(self, *_args):
                    yield StreamEvent("text_delta", {"delta": "x", "accumulated": "x"})
                    yield StreamEvent("response_completed")

            def _agent() -> Agent:
                agent = Agent(AgentConfig(model="test-model", system_prompt="s",
                                           tools=ToolRegistry()))
                agent._provider = _StaticTextProvider()
                return agent

            artifacts = LocalArtifactStore(tmp)
            deps = PipelineDeps(
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF,
                llm_sem=asyncio.Semaphore(16), retrieval_sem=asyncio.Semaphore(4),
                gap_miner_agent=_agent(), novelty_agent=_agent(), domain_review_agent=_agent(),
                model=None,
            )
            checkpointer = InMemorySaver()
            thread_id = "already-completed-thread"
            problem = ResearchProblemInput(question="q", domain="biology",
                                            objective="o", evidence_texts=["e"])

            # 空候选快速路径（gap_mining -> generate -> collect -> rank -> END），跑一次到底
            async def fake_mine(*a, **kw):
                return []
            async def fake_generate(*a, **kw):
                return []
            orig_mine, orig_gen = graph_mod.mine_research_gaps, graph_mod.generate_candidates
            graph_mod.mine_research_gaps = fake_mine
            graph_mod.generate_candidates = fake_generate
            try:
                first_results, first_ranking = await run_graph(
                    problem, deps=deps, thread_id=thread_id, checkpointer=checkpointer)
                self.assertEqual([], first_results)
                self.assertEqual([], first_ranking)

                with self.assertRaises(ValueError):
                    await run_graph(problem, deps=deps, thread_id=thread_id,
                                     checkpointer=checkpointer)
            finally:
                graph_mod.mine_research_gaps = orig_mine
                graph_mod.generate_candidates = orig_gen

    async def test_default_build_has_no_checkpointer(self) -> None:
        # 原名 test_no_thread_id_means_no_checkpointer 对不上：build_compiled_graph 本身
        # 只收 checkpointer，不收 thread_id（thread_id 是 run_graph 的参数，用来定位
        # checkpointer 里的哪条线程）——改名反映实际测的是什么
        from athena.workflows.search.graph import build_compiled_graph

        self.assertIsNone(build_compiled_graph().checkpointer)


def _problem() -> ResearchProblemInput:
    return ResearchProblemInput(question="q", domain="biology", objective="o",
                                 evidence_texts=["e"])


def _deps() -> PipelineDeps:
    # 用 mkdtemp 而非 TemporaryDirectory：这个 helper 直接内联在 run_graph(...) 调用里
    # 用（`run_graph(_problem(), deps=_deps(), ...)`），没有外层 with 块能接住上下文管理器；
    # 目录不清理是可接受的测试副作用，不是需要"修好"的东西
    import tempfile

    from athena.core.agent import Agent, AgentConfig, StreamEvent
    from athena.core.tool import ToolRegistry
    from athena.storage.artifact_store import LocalArtifactStore

    class _StaticTextProvider:
        async def stream(self, *_args):
            yield StreamEvent("text_delta", {"delta": "x", "accumulated": "x"})
            yield StreamEvent("response_completed")

    def _agent() -> Agent:
        agent = Agent(AgentConfig(model="test-model", system_prompt="s", tools=ToolRegistry()))
        agent._provider = _StaticTextProvider()
        return agent

    artifacts = LocalArtifactStore(tempfile.mkdtemp())
    return PipelineDeps(
        artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF,
        llm_sem=asyncio.Semaphore(16), retrieval_sem=asyncio.Semaphore(4),
        gap_miner_agent=_agent(), novelty_agent=_agent(), domain_review_agent=_agent(),
        model=None,
    )


class EventBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def _patched_empty_run(self):
        # 两条用例都只关心事件桥接本身，不关心候选内容——把 gap_mining/generate patch 成
        # 立即成功、产出空候选，走 Task 2 已验证过的空候选快速路径
        # (gap_mining -> generate -> collect -> rank -> END)，四个节点各触发一次 step 事件
        import athena.workflows.search.graph as graph_mod

        async def fake_mine(*a, **kw):
            return []
        async def fake_generate(*a, **kw):
            return []

        orig_mine = graph_mod.mine_research_gaps
        orig_gen = graph_mod.generate_candidates
        graph_mod.mine_research_gaps = fake_mine
        graph_mod.generate_candidates = fake_generate
        self.addCleanup(setattr, graph_mod, "mine_research_gaps", orig_mine)
        self.addCleanup(setattr, graph_mod, "generate_candidates", orig_gen)

    async def test_nodes_emit_started_and_completed(self) -> None:
        # 对齐 paper_scout 立的约定：<module>/started|step|completed
        await self._patched_empty_run()
        events = []
        async def emit(kind, ref, data=None):
            events.append((kind, ref, data))
        await run_graph(_problem(), deps=_deps(), emit=emit)
        kinds = {kind for kind, _ref, _data in events}
        self.assertIn("idea_generation/started", kinds)
        self.assertIn("idea_generation/completed", kinds)
        step_nodes = {data["node"] for kind, _ref, data in events
                      if kind == "idea_generation/step"}
        self.assertEqual({"gap_mining", "generate", "collect", "rank"}, step_nodes)

    async def test_events_carry_no_payload(self) -> None:
        # 硬约束：事件只带小摘要与 ArtifactRef，不带 payload——不能出现结果列表、候选对象
        # 这类大对象；ref 必须是字符串，顶层 step 事件的 data 只能有 node 这一个 key。
        #
        # 本用例跑的是空候选快速路径，看不到候选级 step 事件（那种多一个 candidate_index）。
        # 候选级事件的 payload 形状由
        # test_candidate_subgraph_emits_step_events_with_candidate_index 覆盖——不要在这里
        # 放宽断言去兼容它，那样两种形状都只剩一条宽松断言在守。
        await self._patched_empty_run()
        events = []
        async def emit(kind, ref, data=None):
            events.append((kind, ref, data))
        await run_graph(_problem(), deps=_deps(), emit=emit)
        for _kind, ref, _data in events:
            self.assertIsInstance(ref, str)
        step_events = [e for e in events if e[0] == "idea_generation/step"]
        self.assertTrue(step_events)
        for _kind, _ref, data in step_events:
            self.assertEqual({"node"}, set(data.keys()))
            self.assertIsInstance(data["node"], str)
        for kind, _ref, data in events:
            if kind != "idea_generation/step":
                self.assertIsNone(data)

    async def test_candidate_subgraph_emits_step_events_with_candidate_index(self) -> None:
        # Task 7 遗留缺口（最终整分支审查 I4）：候选段原来是手动 ainvoke，screen/novelty/
        # 三视角/辩论闭环这些子步骤完全没有事件，一个候选从头到尾在事件流上只是一条不透明
        # 的 "candidate" 记录。这条测试证明候选子图内部节点现在也会发事件，且带着
        # candidate_index 供并发场景下区分是哪个候选——把 candidate_node 里 deps.emit
        # 那个分支删掉，这条测试会失败：candidate_steps 会是空列表
        import athena.workflows.search.graph as graph_mod
        from athena.workflows.search.idea_schemas import HypothesisPackage

        async def fake_mine(*a, **kw):
            return []

        async def fake_generate(*a, **kw):
            return [HypothesisPackage(
                idea_id="idea-1", generation_strategy="s", novel_hypothesis="h",
                supported_premises=[], inference_chain=[], predicted_observations=["p"],
                disconfirming_observations=["d"], lineage_op="generate",
            )]

        async def fake_falsifiability_check(*a, **kw):
            raise RuntimeError("forced degrade")

        orig_mine = graph_mod.mine_research_gaps
        orig_gen = graph_mod.generate_candidates
        orig_fc = graph_mod.falsifiability_check
        graph_mod.mine_research_gaps = fake_mine
        graph_mod.generate_candidates = fake_generate
        graph_mod.falsifiability_check = fake_falsifiability_check
        self.addCleanup(setattr, graph_mod, "mine_research_gaps", orig_mine)
        self.addCleanup(setattr, graph_mod, "generate_candidates", orig_gen)
        self.addCleanup(setattr, graph_mod, "falsifiability_check", orig_fc)

        events = []

        async def emit(kind, ref, data=None):
            events.append((kind, ref, data))

        await run_graph(_problem(), deps=_deps(), emit=emit)

        candidate_steps = [
            (kind, ref, data) for kind, ref, data in events
            if kind == "idea_generation/step" and data.get("node") == "screen"
        ]
        self.assertEqual(1, len(candidate_steps))
        _kind, ref, data = candidate_steps[0]
        self.assertEqual(0, data["candidate_index"])
        self.assertIn("candidate[0]", ref)

        # I6：候选级 step 事件此前完全不受"事件只带小摘要"这条硬约束的守卫——
        # test_events_carry_no_payload 断言的是 data 的 key 集合恰好为 {"node"}，但它跑的是
        # 空候选路径，从来没见过候选级事件这种多带一个 candidate_index 的形状。这里补上：
        # 候选级 step 事件的 key 集合必须恰好是 {node, candidate_index}，两个值都是小标量，
        # 谁往里塞 package/reviews 这类大对象都会在这里变红。
        for _k, event_ref, event_data in events:
            self.assertIsInstance(event_ref, str)
        for _k, _r, event_data in [e for e in events if e[0] == "idea_generation/step"
                                   and "candidate_index" in (e[2] or {})]:
            self.assertEqual({"node", "candidate_index"}, set(event_data.keys()))
            self.assertIsInstance(event_data["node"], str)
            self.assertIsInstance(event_data["candidate_index"], int)

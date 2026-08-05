"""Idea Generation 的图装配。

**本文件是唯一允许 import langgraph 的业务文件。** 领域函数（review_board / evidence_retrieval /
revision / gatekeeper 里的那些）对图无感知，节点只是薄适配器：读 State → 调既有领域函数 →
返回 State 补丁。这样挂在领域函数上的单元测试不受编排重构影响。
"""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import get_runtime
from langgraph.types import Send
from pydantic_ai.models import Model

from athena.research.ranking import HypoPriList, PairwiseComparison, RankedCandidate
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    PAIRWISE_JUDGE_SYSTEM_PROMPT,
    PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.candidate_generation import (
    MAX_VERBALIZED_SAMPLES,
    deduplicate_candidates,
    generate_candidates,
)
from athena.workflows.search.evidence_retrieval import (
    collect_novelty_evidence,
    degraded_novelty_report,
    limited_by,
    mine_research_gaps,
)
from athena.workflows.search.gatekeeper import hard_gate, pre_gate
from athena.workflows.search.idea_schemas import (
    GateVerdict,
    HypothesisPackage,
    PairwiseJudgment,
    PipelineCandidateResult,
    ResearchProblemInput,
    RevisionDraft,
    RevisionRound,
    SkepticJudgment,
    SkepticReport,
)
from athena.workflows.search.pre_gate_checks import (
    degraded_falsifiability_report,
    falsifiability_check,
    structural_check,
)
from athena.workflows.search.review_board import (
    REVIEW_PERSPECTIVES,
    build_perspective_input,
    read_prior_transcript,
    review_or_degrade,
)
from athena.workflows.search.revision import (
    MAX_DEBATE_ROUNDS,
    blocked_item_cleared,
    build_rereview_prompt,
    is_no_op_revision,
    is_revisable,
    refresh_stale_evidence,
    revise_candidate,
    select_debate_opponent,
)
from athena.workflows.search.state import CandidateState, PipelineDeps, PipelineState
from athena.workflows.search.validation import match_verifier, plan_validation


# ====== 节点 ======

async def gap_mining_node(state: PipelineState) -> dict:
    """步骤 [2]：空白挖掘。

    Example:
        >>> await gap_mining_node({"problem": problem})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    gaps = await mine_research_gaps(
        state["problem"], agent=deps.gap_miner_agent, corpus_ref=deps.corpus_ref,
        artifacts=deps.artifacts, model=deps.model,
    )
    return {"gaps": gaps}


async def generate_node(state: PipelineState) -> dict:
    """步骤 [3]：多候选生成 + 去重。

    Example:
        >>> await generate_node({"problem": problem, "gaps": []})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    candidates = await generate_candidates(
        state["problem"], state["gaps"],
        sample_size=state.get("sample_size", MAX_VERBALIZED_SAMPLES), model=deps.model,
    )
    return {"candidates": deduplicate_candidates(candidates)}


def fan_out_candidates(state: PipelineState) -> list[Send] | str:
    """把每个候选发到候选子图；index 与 problem 随分支带走，供 collect 恢复输入序、
    候选子图内部使用（match_verifier 等步骤要读 problem.domain，而 CandidateState 只能
    携带 Send 显式带过去的字段，不会自动继承顶层 PipelineState）。

    candidates 为空（VerbalizedSamplingResponse 明确允许 candidates=[]，全灭是合法结果不是
    错误）时直接路由到 "collect" 字符串，不发任何 Send——Send 列表为空时 langgraph 不会触发
    下游任何一条分支，"candidate"/"collect"/"rank" 会整条跳过，run_graph 读
    final["ordered_results"] 就会因为 key 缺失而 KeyError。重构前 asyncio.gather([]) 天然
    退化到空列表，这里必须显式路由才能保住同样的优雅退化（实测过：不加这条分支，空候选输入
    会在 run_graph 里 KeyError，而不是回归前的 ([], [])）。

    Example:
        >>> fan_out_candidates({"candidates": [pkg], "problem": problem})  # doctest: +SKIP
        >>> fan_out_candidates({"candidates": [], "problem": problem})  # doctest: +SKIP
        'collect'
    """
    if not state["candidates"]:
        return "collect"
    return [
        Send("candidate", CandidateState(index=i, package=package, problem=state["problem"]))
        for i, package in enumerate(state["candidates"])
    ]


async def collect_node(state: PipelineState) -> dict:
    """按候选输入序重排结果（见下方节点注释说明为何不能依赖 reducer 顺序）。

    写的是 ``ordered_results`` 而不是 ``results``：``results`` 挂的是 operator.add 累加
    reducer，专门用来收 Send 各分支的产出，如果 collect 把排好序的结果写回同一个 key，
    reducer 会把它加到已经累积的值上面，导致每条结果翻倍（实测：单候选跑一遍 results 里出现
    两条一样的记录）。``ordered_results`` 是没挂 reducer 的普通 State 字段（默认整体覆盖），
    只在这里写一次，天然不会被叠加。

    state.get("results", []) 而不是 state["results"]：candidates 为空时 fan_out_candidates
    直接路由到这里，没有任何 candidate 分支跑过，"results" 这个 key 从未被写入过，直接下标
    访问会 KeyError。

    Example:
        >>> await collect_node({"results": [(1, r1), (0, r0)]})  # doctest: +SKIP
    """
    ordered = sorted(state.get("results", []), key=lambda pair: pair[0])
    return {"ordered_results": [result for _index, result in ordered]}


# ====== 候选子图节点（Task 3：候选段细化） ======
#
# 领域函数（pre_gate_checks / evidence_retrieval / review_board / validation / gatekeeper /
# revision 里的那些）一个未动，下面这些节点全部是"读 CandidateState -> 调既有领域函数 ->
# 返回补丁"的薄适配器。三个审阅视角节点刻意各写一份而不是共用一个参数化工厂——三份高度相似
# 在这个规模是预期的，强行抽象只会让"这是三个独立的图节点"这件事变得不直观。

async def screen_node(state: CandidateState) -> dict:
    """步骤 [4]：结构检查 + 可证伪性审计 + pre_gate。

    falsifiability_check 失败按不可证伪处理而非上抛：没有证据不能算通过，交给 pre_gate 判
    REVISE，不静默放行也不拖垮这个候选。

    Example:
        >>> await screen_node({"package": pkg})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    package = state["package"]
    structural = structural_check(package)
    try:
        async with limited_by(deps.llm_sem):
            falsifiability = await falsifiability_check(package, model=deps.model)
    except Exception as error:  # noqa: BLE001 - provider 报错形态不定，一律降级
        falsifiability = degraded_falsifiability_report(package.idea_id, error)
    return {
        "structural": structural, "falsifiability": falsifiability,
        "decision": pre_gate(structural, falsifiability),
    }


def route_after_screen(state: dict) -> str:
    """pre_gate 未过（REVISE）的候选直接路由到 END，跳过 [5]-[8] 的昂贵段；PASS 才进 novelty。

    Example:
        >>> route_after_screen({"decision": pass_decision})  # doctest: +SKIP
        'novelty'
    """
    decision = state["decision"]
    return "novelty" if decision.verdict == GateVerdict.PASS else END


async def novelty_node(state: CandidateState) -> dict:
    """步骤 [5]：数值性审计，调 collect_novelty_evidence 采集新颖性证据。

    检索失败降级为空报告而不是上抛，不拖垮这个候选：facet_overlap 为空会命中 hard_gate
    既有的"空 facet 判 REVISE"逻辑，无需另写判定。

    Example:
        >>> await novelty_node({"package": pkg})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    package = state["package"]
    try:
        novelty = await collect_novelty_evidence(
            package, agent=deps.novelty_agent, artifacts=deps.artifacts,
            corpus_ref=deps.corpus_ref, llm_sem=deps.llm_sem, retrieval_sem=deps.retrieval_sem,
            model=deps.model,
        )
    except Exception as error:  # noqa: BLE001 - 检索失败降级为空报告，不拖垮这个候选
        novelty = await degraded_novelty_report(package.idea_id, error, deps.artifacts)
    return {"novelty": novelty}


async def review_methodology_node(state: CandidateState) -> dict:
    """[6] 视角 methodology：固定绑定 REVIEW_PERSPECTIVES[0]。

    调 review_or_degrade（review_board.py 里 review_board 本身用的同一个 fail-closed 包装：
    单次尝试，失败就标 failed=True）而不是裸调 review_one_perspective——三个视角本就并行
    展开成图节点了，若某一个视角瞬时失败直接原样上抛，会让整个候选（乃至同批其它候选，视
    langgraph 异常传播范围而定）连带失败，这正是 review_board 现有 fail-closed 设计要防的
    "审阅没跑成不能拖垮整批候选"。

    Example:
        >>> await review_methodology_node({"package": pkg, "novelty": novelty})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    report = await review_or_degrade(
        state["package"], REVIEW_PERSPECTIVES[0], novelty=state["novelty"],
        domain_review_agent=deps.domain_review_agent, artifacts=deps.artifacts,
        corpus_ref=deps.corpus_ref, llm_sem=deps.llm_sem, retrieval_sem=deps.retrieval_sem,
        model=deps.model,
    )
    return {"review_reports": [report]}


async def review_statistics_node(state: CandidateState) -> dict:
    """[6] 视角 statistics：固定绑定 REVIEW_PERSPECTIVES[1]，逻辑同 review_methodology_node。

    Example:
        >>> await review_statistics_node({"package": pkg, "novelty": novelty})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    report = await review_or_degrade(
        state["package"], REVIEW_PERSPECTIVES[1], novelty=state["novelty"],
        domain_review_agent=deps.domain_review_agent, artifacts=deps.artifacts,
        corpus_ref=deps.corpus_ref, llm_sem=deps.llm_sem, retrieval_sem=deps.retrieval_sem,
        model=deps.model,
    )
    return {"review_reports": [report]}


async def review_domain_consistency_node(state: CandidateState) -> dict:
    """[6] 视角 domain_consistency：固定绑定 REVIEW_PERSPECTIVES[2]，唯一配了 paper_rag
    检索循环的视角，逻辑同另外两个视角节点。

    Example:
        >>> await review_domain_consistency_node({"package": pkg, "novelty": novelty})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    report = await review_or_degrade(
        state["package"], REVIEW_PERSPECTIVES[2], novelty=state["novelty"],
        domain_review_agent=deps.domain_review_agent, artifacts=deps.artifacts,
        corpus_ref=deps.corpus_ref, llm_sem=deps.llm_sem, retrieval_sem=deps.retrieval_sem,
        model=deps.model,
    )
    return {"review_reports": [report]}


async def validation_node(state: CandidateState) -> dict:
    """步骤 [7] + 三视角汇聚屏障（defer=True）：调 match_verifier + plan_validation，并把
    validation_plan_ref 落到 package 上——`plan_ref = await artifacts.put_text(...)` +
    `package.model_copy(...)` 这一步不是可选项，validation_plan_ref 必须指向
    ValidationPlan 本身而不是复用成本估计的引用。

    review_reports 是三个审阅节点并发写入的 operator.add 累加字段，这里收拢成 reviews
    （普通覆盖字段，供 gate_node/revise_node/rereview_node 读写）——两字段拆分的原因见
    state.CandidateState 的字段注释。

    Example:
        >>> await validation_node({"package": pkg, "problem": problem,
        ...     "review_reports": [r1, r2, r3]})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    package = state["package"]
    verifier = match_verifier(package, state["problem"].domain)
    validation_plan = await plan_validation(package, verifier, artifacts=deps.artifacts)
    plan_ref = await deps.artifacts.put_text(validation_plan.model_dump_json())
    package = package.model_copy(update={"validation_plan_ref": plan_ref})
    return {
        "package": package, "validation_plan": validation_plan,
        "reviews": list(state.get("review_reports", [])),
    }


async def gate_node(state: CandidateState) -> dict:
    """步骤 [8]：hard_gate 终审（修订闭环之前的第一次）。

    Example:
        >>> await gate_node({"structural": s, "falsifiability": f, "novelty": n,
        ...     "reviews": revs, "validation_plan": plan})  # doctest: +SKIP
    """
    decision = hard_gate(
        state["structural"], state["falsifiability"], state["novelty"],
        state["reviews"], state["validation_plan"],
    )
    return {"decision": decision}


def route_after_gate(state: dict) -> str:
    """is_revisable(decision) 为真时进入修订闭环，否则直接结束——不可修订的裁决
    （比如 novelty_ok 拦截）没有辩论的余地，跑修订闭环只会浪费 LLM 调用。

    Example:
        >>> route_after_gate({"decision": decision})  # doctest: +SKIP
        'revise'
    """
    return "revise" if is_revisable(state["decision"]) else END


async def revise_node(state: CandidateState) -> dict:
    """辩论一轮的前半段：选定/复用对手视角，跑一次修订，判断是否 no-op。

    debated_perspective 只在候选分支第一次进 revise 时选定并缓存——run_debate 里
    select_debate_opponent 只在循环外调一次，之后每轮复用同一个对手，不随 reviews 更新重选
    （risk_total 的选择结果依赖 reviews 内容，重选可能在中途换对手，这是行为差异，不是实现
    细节）。prior_transcript 同理只在第一次计算并缓存。

    reviser 失败（revise_candidate 单次调用即向外抛，不重试）直接结束辩论——package 原样
    不动，与 run_debate 的 try/except: break 等价。

    no-op 修订（is_no_op_revision 为真）：package 同样不推进（revise_candidate 产出的
    revised 被丢弃，不写回 state），但要追加一条 cleared=False、reviewer_response_ref=None
    的 RevisionRound——这是 run_debate 里"no-op 也留痕"的那条记录。真实修订才把 package
    推进到 revised 并把 draft 存进 pending_draft 供 rereview_node 用。

    revision_round_before 只在辩论第一次进 revise 时缓存，不随每轮重算——route_after_revise
    靠它判断"这条候选有没有*曾经*被真的修订过"，这是决定辩论结束后要不要 refresh 的问题，
    跟"这一轮有没有产出新草稿"（下面用 pending_draft 回答）是两个不同的问题，不能共用同一个
    信号：例如第 2 轮 reviser 失败或 no-op 时，package.revision_round 仍停留在第 1 轮成功
    时的值，若 revision_round_before 也随每轮重算，两者会被误判为相等，看起来像"从未推进"，
    实际上第 1 轮确实推进过。

    pending_draft 在失败/no-op 分支显式清成 None——不清的话，某一轮失败或 no-op 时会残留
    上一轮成功产出的旧 pending_draft，route_after_revise 会误以为这一轮又产出了新草稿，拿
    旧草稿再发起一次多余的 rereview。

    Example:
        >>> await revise_node({"package": pkg, "reviews": revs, "decision": decision})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    package = state["package"]
    reviews = state["reviews"]
    decision = state["decision"]
    revision_round_before = state.get("revision_round_before")
    if revision_round_before is None:
        revision_round_before = package.revision_round

    debated_perspective = state.get("debated_perspective") or select_debate_opponent(
        decision.blocking_factor, reviews)
    prior_transcript = state.get("prior_transcript")
    if prior_transcript is None:
        prior_transcript = await read_prior_transcript(state["novelty"], deps.artifacts)
    prior_summaries = state.get("prior_summaries", [])
    round_index = len(state.get("revisions", [])) + 1

    try:
        revised, draft = await revise_candidate(
            package, blocking_factor=decision.blocking_factor,
            debated_perspective=debated_perspective, reviews=reviews,
            prior_rounds=prior_summaries, llm_sem=deps.llm_sem, model=deps.model,
        )
    except Exception:  # noqa: BLE001 - 与 run_debate 的 try/except: break 等价，reviser 失败结束辩论
        return {
            "revision_round_before": revision_round_before,
            "debated_perspective": debated_perspective, "prior_transcript": prior_transcript,
            "pending_draft": None,  # 清掉可能残留的上一轮草稿，避免 route_after_revise 用旧草稿误路由到 rereview
        }

    rebuttal_ref = await deps.artifacts.put_text(draft.rebuttal)
    if is_no_op_revision(draft, package):
        round_record = RevisionRound(
            round_index=round_index, debated_perspective=debated_perspective,
            package_ref=await deps.artifacts.put_text(package.model_dump_json()),
            rebuttal_ref=rebuttal_ref, reviewer_response_ref=None, cleared=False,
        )
        return {
            "revision_round_before": revision_round_before,
            "debated_perspective": debated_perspective, "prior_transcript": prior_transcript,
            "revisions": [round_record],
            "pending_draft": None,  # 同上：no-op 也要清掉，理由相同
        }

    return {
        "package": revised, "pending_draft": draft,
        "debated_perspective": debated_perspective, "prior_transcript": prior_transcript,
        "revision_round_before": revision_round_before,
    }


def route_after_revise(state: dict) -> str:
    """两个独立的问题：pending_draft 非 None 说明这一轮真的产出了新草稿，去 rereview；
    否则这一轮没有产出（reviser 失败或 no-op），辩论到此为止——再看
    package.revision_round 有没有比进入辩论前（revision_round_before，只在辩论开始时
    缓存一次）更大，决定是去 refresh（这条候选之前至少有一轮真的被修订过）还是直接
    END（从未被修订过，比如第一轮就失败/no-op）。

    Example:
        >>> route_after_revise({"package": pkg, "pending_draft": draft})  # doctest: +SKIP
        'rereview'
    """
    if state.get("pending_draft") is not None:
        return "rereview"
    package = state["package"]
    if package.revision_round > state.get("revision_round_before", package.revision_round):
        return "refresh"
    return END


async def rereview_node(state: CandidateState) -> dict:
    """辩论一轮的后半段：对手对本轮修订稿重表态。只在 route_after_revise 判定"真的推进了"
    之后才会跑到这里，pending_draft 因此保证非 None。

    对手重表态失败：与 run_debate 一致的 fail-closed 降级——修订稿已经构造成功，package
    仍推进到 revised（在 revise_node 里已经推进过了，这里不再改 package），只把对手这份
    报告标 failed=True，cleared 交给 blocked_item_cleared 自然判 False；异常不向外传播。
    同时置 rereview_failed=True——run_debate 里这条路径是 try/except: break，辩论到此
    立即结束；单靠 cleared=False 会和"对手正常回应但没清除"这个还要继续辩论的情形混淆，
    让 route_after_rereview 误以为轮次未满、拿这一轮的旧 pending_draft 再回 revise 多打一轮。

    Example:
        >>> await rereview_node({"package": pkg, "reviews": revs,
        ...     "pending_draft": draft, "debated_perspective": "methodology"})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    package = state["package"]
    reviews = state["reviews"]
    draft = state["pending_draft"]
    opponent_id = state["debated_perspective"]
    prior_transcript = state.get("prior_transcript", "")
    round_index = len(state.get("revisions", [])) + 1

    previous = next(r for r in reviews if r.perspective == opponent_id)
    perspective = next(p for p in REVIEW_PERSPECTIVES if p.perspective_id == opponent_id)
    rebuttal_ref = await deps.artifacts.put_text(draft.rebuttal)
    prompt = build_rereview_prompt(
        package, perspective, previous, draft,
        round_index=round_index, prior_transcript=prior_transcript,
    )
    try:
        async with limited_by(deps.llm_sem):
            judgment = await single_turn_chat(prompt, SkepticJudgment, model=deps.model)
    except Exception as error:  # noqa: BLE001 - 对手重表态失败，沿用 review_board 的 fail-closed 降级
        failed_report = SkepticReport(
            idea_id=package.idea_id, perspective=opponent_id,
            critique=f"opponent re-review failed: {error}", unaddressed_risks=[],
            fatal_flaw_found=False, failed=True,
        )
        updated_reviews = [failed_report if r.perspective == opponent_id else r for r in reviews]
        round_record = RevisionRound(
            round_index=round_index, debated_perspective=opponent_id,
            package_ref=await deps.artifacts.put_text(package.model_dump_json()),
            rebuttal_ref=rebuttal_ref, reviewer_response_ref=None, cleared=False,
        )
        return {
            "reviews": updated_reviews, "revisions": [round_record], "cleared": False,
            "rereview_failed": True,
        }

    canonical_ref = await deps.artifacts.put_text(build_perspective_input(
        package, perspective, corpus_ref=deps.corpus_ref, prior_transcript=prior_transcript))
    response = SkepticReport(
        idea_id=package.idea_id, perspective=opponent_id, critique=judgment.critique,
        unaddressed_risks=judgment.unaddressed_risks, fatal_flaw_found=judgment.fatal_flaw_found,
        transcript_ref=previous.transcript_ref, input_ref=canonical_ref,
    )
    updated_reviews = [response if r.perspective == opponent_id else r for r in reviews]
    cleared = blocked_item_cleared(state["decision"].blocking_factor, updated_reviews)
    round_record = RevisionRound(
        round_index=round_index, debated_perspective=opponent_id,
        package_ref=await deps.artifacts.put_text(package.model_dump_json()),
        rebuttal_ref=rebuttal_ref,
        reviewer_response_ref=await deps.artifacts.put_text(judgment.model_dump_json()),
        cleared=cleared,
    )
    prior_summary = (
        f"round {round_index}: rebuttal={draft.rebuttal} | "
        f"reviewer replied={judgment.critique} | remaining risks={judgment.unaddressed_risks}"
    )
    return {
        "reviews": updated_reviews, "revisions": [round_record], "cleared": cleared,
        "prior_summaries": state.get("prior_summaries", []) + [prior_summary],
    }


def route_after_rereview(state: dict) -> str:
    """对手重表态失败：辩论到此为止，直接去 refresh——与 run_debate 的
    try/except: break 等价，不再像 cleared=False 那样可能触发下一轮。cleared 或轮次
    已达上限同样去 refresh；否则回 revise 再来一轮。

    Example:
        >>> route_after_rereview({"rereview_failed": True})  # doctest: +SKIP
        'refresh'
    """
    if state.get("rereview_failed"):
        return "refresh"
    if state.get("cleared") or len(state.get("revisions", [])) >= MAX_DEBATE_ROUNDS:
        return "refresh"
    return "revise"


async def refresh_node(state: CandidateState) -> dict:
    """终局刷新：辩论结束后只重跑输入已确实失效的报告（顺序、判据见 refresh_stale_evidence
    自身的文档字符串），为紧随其后的终审 regate_node 准备一套自洽的证据。

    Example:
        >>> await refresh_node({"package": pkg, "problem": problem, "novelty": novelty,
        ...     "reviews": revs})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    package, structural, falsifiability, novelty, reviews, validation_plan = (
        await refresh_stale_evidence(
            state["package"], problem_domain=state["problem"].domain,
            novelty=state["novelty"], reviews=state["reviews"],
            novelty_agent=deps.novelty_agent, domain_review_agent=deps.domain_review_agent,
            artifacts=deps.artifacts, corpus_ref=deps.corpus_ref, llm_sem=deps.llm_sem,
            retrieval_sem=deps.retrieval_sem, model=deps.model,
        )
    )
    return {
        "package": package, "structural": structural, "falsifiability": falsifiability,
        "novelty": novelty, "reviews": reviews, "validation_plan": validation_plan,
    }


async def regate_node(state: CandidateState) -> dict:
    """终审：辩论后的第二次 hard_gate。revision_blocking_factor 取 gate_node 那次 decision 的
    blocking_factor（循环全程没有节点改写 state["decision"]，它天然保持在进入辩论前的那个
    值）——靠"没人碰它"而不是显式局部变量来保持这个值不变。

    Example:
        >>> await regate_node({"structural": s, "falsifiability": f, "novelty": n,
        ...     "reviews": revs, "validation_plan": plan, "decision": decision})  # doctest: +SKIP
    """
    decision = hard_gate(
        state["structural"], state["falsifiability"], state["novelty"],
        state["reviews"], state["validation_plan"],
    )
    return {"decision": decision, "revision_blocking_factor": state["decision"].blocking_factor}


def build_candidate_graph() -> StateGraph:
    """装配候选子图：screen ->[route_after_screen]-> novelty|END；novelty 后三个审阅视角节点
    并行展开，汇于 validation（defer=True 汇聚屏障）；validation -> gate ->[route_after_gate]->
    revise|END。修订闭环是 revise/rereview 之间的条件边循环：route_after_revise 三路分流
    （rereview：本轮产出新草稿；refresh：本轮没产出但之前某轮真的推进过；END：从未推进过），
    route_after_rereview 二路分流（refresh：清除/对手重表态失败/轮次已满；revise：继续下一
    轮），最终都汇到 refresh -> regate -> END。

    Example:
        >>> build_candidate_graph().compile()  # doctest: +SKIP
    """
    graph = StateGraph(CandidateState, context_schema=PipelineDeps)
    graph.add_node("screen", screen_node)
    graph.add_node("novelty", novelty_node)
    graph.add_node("review_methodology", review_methodology_node)
    graph.add_node("review_statistics", review_statistics_node)
    graph.add_node("review_domain_consistency", review_domain_consistency_node)
    graph.add_node("validation", validation_node, defer=True)
    graph.add_node("gate", gate_node)
    graph.add_node("revise", revise_node)
    graph.add_node("rereview", rereview_node)
    graph.add_node("refresh", refresh_node)
    graph.add_node("regate", regate_node)

    graph.add_edge(START, "screen")
    graph.add_conditional_edges("screen", route_after_screen, ["novelty", END])
    graph.add_edge("novelty", "review_methodology")
    graph.add_edge("novelty", "review_statistics")
    graph.add_edge("novelty", "review_domain_consistency")
    graph.add_edge("review_methodology", "validation")
    graph.add_edge("review_statistics", "validation")
    graph.add_edge("review_domain_consistency", "validation")
    graph.add_edge("validation", "gate")
    graph.add_conditional_edges("gate", route_after_gate, ["revise", END])
    graph.add_conditional_edges("revise", route_after_revise, ["rereview", "refresh", END])
    graph.add_conditional_edges("rereview", route_after_rereview, ["refresh", "revise"])
    graph.add_edge("refresh", "regate")
    graph.add_edge("regate", END)
    return graph


_COMPILED_CANDIDATE_GRAPH = None
"""候选子图的编译结果缓存，惰性构造一次后被所有候选分支复用。图结构是静态的（不随候选内容
变化），candidate_node 每个候选跑一次、候选数可能有几十个，重复编译除了浪费 CPU 没有别的
作用——用模块级变量 + 惰性初始化而不是在 import 时就编译，避免给"import graph.py"这个动作
本身增加不必要的副作用（比如测试只想 import 某个纯函数，不想连带触发一次图编译）。"""


def _compiled_candidate_graph():
    """惰性获取（并按需构造）候选子图的编译单例，见 _COMPILED_CANDIDATE_GRAPH 的模块级注释。

    Example:
        >>> _compiled_candidate_graph() is _compiled_candidate_graph()  # doctest: +SKIP
        True
    """
    global _COMPILED_CANDIDATE_GRAPH
    if _COMPILED_CANDIDATE_GRAPH is None:
        _COMPILED_CANDIDATE_GRAPH = build_candidate_graph().compile()
    return _COMPILED_CANDIDATE_GRAPH


async def candidate_node(state: CandidateState) -> dict:
    """候选段：把 CandidateState 交给候选子图（Task 3，build_candidate_graph）跑完整条
    [4]-[8]+修订闭环，再把子图终态收拢成 (index, PipelineCandidateResult) 元组写回顶层
    results 累加器。

    **不能把已编译子图直接注册成顶层 "candidate" 节点**（即
    `graph.add_node("candidate", build_candidate_graph().compile())`）：子图与顶层图是两套
    不同的 State schema（CandidateState vs PipelineState），Send 把 CandidateState 形状的
    payload 直接交给子图执行，子图跑完后的终态也是 CandidateState 形状，不是
    "results" 元组；langgraph 只在父子图共享同名同 reducer 的 channel 上做合并
    （CandidateState 没有声明 "results" 这个 channel），子图产出的
    package/decision/reviews 等字段会被子图自己的 Pregel 直接丢弃，collect_node 读到的
    results 会恒为空（已用一个独立的最小 langgraph 脚本实测过这个行为，不是猜测）。因此这里
    显式 ainvoke 子图、再手动转换成顶层认识的 "results" 更新。

    子图本身通过 _compiled_candidate_graph() 惰性编译一次、跨候选复用，不在每个候选分支上
    重新 build_candidate_graph().compile()——图结构不随候选内容变化，重复编译只是浪费。

    context=deps 显式传给子图 ainvoke/astream：子图节点内部同样用 get_runtime(PipelineDeps)
    取依赖，不显式传的话子图自己的 runtime context 不会被设置。

    deps.emit 非 None 时走 astream 而不是 ainvoke——顶层 run_graph 的 emit 只在顶层节点边界
    发事件（gap_mining/generate/candidate/collect/rank），"candidate" 这一条对单个候选而言
    是个不透明整体，screen/novelty/三视角/辩论闭环这些子步骤原来完全没有事件（Task 7 遗留的
    已知缺口）。事件的 ref/data 里带上 state["index"]：多个候选并发跑在同一个 emit 回调上，
    不带候选下标的话，读事件流的一方分不清"哪个候选的 screen 完成了"。

    **不依赖 reducer 的合并顺序**：Send 分支的完成顺序不受控，而 Elo 是在线增量更新，喂入顺序
    直接改评分。重构前这层保序是 asyncio.gather 白送的（设计 §4.4）；换成 Send 之后没有白送
    的保序了，靠 state["index"] 随分支带走、collect_node 按它显式重排。

    Example:
        >>> await candidate_node({"index": 0, "package": pkg, "problem": problem})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    compiled = _compiled_candidate_graph()
    if deps.emit is None:
        final = await compiled.ainvoke(dict(state), context=deps)
    else:
        final = {}
        async for mode, chunk in compiled.astream(
            dict(state), context=deps, stream_mode=["updates", "values"],
        ):
            if mode == "updates":
                for node_name in chunk:
                    await deps.emit(
                        "idea_generation/step", f"ev:candidate[{state['index']}]:{node_name}",
                        {"node": node_name, "candidate_index": state["index"]},
                    )
            else:
                final = chunk
    result = PipelineCandidateResult(
        package=final["package"], structural=final["structural"],
        falsifiability=final["falsifiability"], novelty=final.get("novelty"),
        reviews=final.get("reviews", []), validation_plan=final.get("validation_plan"),
        decision=final["decision"], revisions=final.get("revisions", []),
        revision_blocking_factor=final.get("revision_blocking_factor"),
    )
    return {"results": [(state["index"], result)]}


async def pairwise_compare(
    package_a: HypothesisPackage, package_b: HypothesisPackage, *,
    llm_sem: asyncio.Semaphore | None = None, model: Model | str | None = None,
) -> PairwiseComparison:
    """轻量 PairwiseJudge：匿名化两个候选、双向各跑一次，规避 position/verbosity/
    self-preference 偏见（呼应设计文档第5节引用的 LLM-as-judge 偏见研究）。双向结果一致时
    直接采信；不一致时以正向结果为准，但把分歧写进 rationale 供审计。

    forward/backward 之间没有依赖，用 gather 并发发起；两次调用各自在自己的协程内部
    ``async with limited_by(llm_sem)`` 获取名额，不在外层整体持有一个名额再等第二个——
    那是候选数 >= 名额数时必然死锁的模式（见 evidence_retrieval.limited_by 的注释）。

    只被 rank_node 调用（Task 4 起图接管编排后，workflow.py 不再有自己的 pairwise 阶段）。

    Example:
        >>> comparison = await pairwise_compare(pkg_a, pkg_b, model=fake_model)  # doctest: +SKIP
        >>> comparison.winner_id in (pkg_a.idea_id, pkg_b.idea_id)
        True
    """
    forward_prompt = "\n\n".join([
        PAIRWISE_JUDGE_SYSTEM_PROMPT,
        PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE.format(
            candidate_a=package_a.novel_hypothesis, candidate_b=package_b.novel_hypothesis,
        ),
    ])
    backward_prompt = "\n\n".join([
        PAIRWISE_JUDGE_SYSTEM_PROMPT,
        PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE.format(
            candidate_a=package_b.novel_hypothesis, candidate_b=package_a.novel_hypothesis,
        ),
    ])

    async def _run(prompt: str) -> PairwiseJudgment:
        """在自己的名额作用域内跑一次 single_turn_chat，供 gather 并发调度。"""
        async with limited_by(llm_sem):
            return await single_turn_chat(prompt, PairwiseJudgment, model=model)

    # 注：forward/backward 任一失败都会在这里直接向外抛，交给 rank_node 外层的
    # gather(..., return_exceptions=True) 兜底跳过整对比较；在这里再吞一次异常只会让外层的
    # 降级逻辑看不到失败发生在哪一侧，没有实际收益。
    forward, backward = await asyncio.gather(_run(forward_prompt), _run(backward_prompt))

    forward_winner = package_a.idea_id if forward.winner == "candidate_a" else package_b.idea_id
    # 反向调用里 candidate_a 对应 package_b，candidate_b 对应 package_a
    backward_winner = package_b.idea_id if backward.winner == "candidate_a" else package_a.idea_id

    if forward_winner == backward_winner:
        winner_id = forward_winner
        rationale = f"forward+backward agree: {forward.rationale} | {backward.rationale}"
    else:
        winner_id = forward_winner
        rationale = (
            f"forward/backward disagreement (possible position bias); forward picked "
            f"{forward_winner} ({forward.rationale}), backward picked {backward_winner} "
            f"({backward.rationale})"
        )
    return PairwiseComparison(
        idea_id_a=package_a.idea_id, idea_id_b=package_b.idea_id, winner_id=winner_id, rationale=rationale,
    )


async def rank_node(state: PipelineState) -> dict:
    """步骤 [9]：存活候选 pairwise Elo 排序。

    比较可以全并发执行，但必须按 (i, j) 索引序喂入 HypoPriList——Elo 是在线增量更新。读的是
    collect_node 写的 ``ordered_results``（已按候选输入序排好，且是普通覆盖字段，不会被
    reducer 叠加），不是 ``results`` 累加器本身。

    Example:
        >>> await rank_node({"ordered_results": [r0]})  # doctest: +SKIP
    """
    deps = get_runtime(PipelineDeps).context
    results = state["ordered_results"]
    survivors = [r for r in results
                 if r.decision.verdict in (GateVerdict.PASS, GateVerdict.EXPLORATORY)]
    book = HypoPriList()
    for survivor in survivors:
        book.ensure_registered(survivor.package.idea_id)

    pairs = [(i, j) for i in range(len(survivors)) for j in range(i + 1, len(survivors))]
    comparisons = await asyncio.gather(*[
        pairwise_compare(survivors[i].package, survivors[j].package,
                         llm_sem=deps.llm_sem, model=deps.model)
        for i, j in pairs
    ], return_exceptions=True)
    for comparison in comparisons:
        if isinstance(comparison, PairwiseComparison):
            book.record_comparison(comparison)
    return {"ranking": book.rank()}


# ====== 图装配 ======

def build_pipeline_graph() -> StateGraph:
    """装配顶层图：gap_mining → generate → Send fan-out → collect → rank。

    Example:
        >>> build_pipeline_graph().compile()  # doctest: +SKIP
    """
    graph = StateGraph(PipelineState, context_schema=PipelineDeps)
    graph.add_node("gap_mining", gap_mining_node)
    graph.add_node("generate", generate_node)
    graph.add_node("candidate", candidate_node)
    graph.add_node("collect", collect_node, defer=True)
    graph.add_node("rank", rank_node)

    graph.add_edge(START, "gap_mining")
    graph.add_edge("gap_mining", "generate")
    graph.add_conditional_edges("generate", fan_out_candidates, ["candidate", "collect"])
    graph.add_edge("candidate", "collect")
    graph.add_edge("collect", "rank")
    graph.add_edge("rank", END)
    return graph


def build_compiled_graph(*, checkpointer: BaseCheckpointSaver | None = None):
    """编译顶层图。checkpointer 为 None 时不启用 checkpoint，行为与重构前一致。

    Example:
        >>> build_compiled_graph().checkpointer is None  # doctest: +SKIP
        True
    """
    return build_pipeline_graph().compile(checkpointer=checkpointer)


async def run_graph(
    problem: ResearchProblemInput,
    *,
    deps: PipelineDeps,
    sample_size: int = MAX_VERBALIZED_SAMPLES,
    thread_id: str | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    emit: Callable[..., Awaitable[None]] | None = None,
) -> tuple[list[PipelineCandidateResult], list[RankedCandidate]]:
    """编译并跑一次图；emit 非 None 时在节点边界发事件。

    传 checkpointer 时必须同时传 thread_id——前者决定存到哪里，后者决定这次跑属于哪条线程；
    只传其一无法定位 checkpoint。

    启用 checkpoint 时，每次调用前先查一次这条 thread 的既有状态：`state.next` 非空说明
    上次在某个节点中断过，这次是续跑，input 必须传 `None`——传非 None 的 input 会被
    langgraph 当成"在这条 thread 上开一次新的运行"，从头重新执行，检查点形同虚设（已实测：
    不这样做会导致已完成的 gap_mining 在续跑时被重新调用一次）。`state.next` 为空且
    `state.values` 也为空（这条 thread 还没跑过）时正常传原始 input；`state.next` 为空但
    `state.values` 非空（这条 thread 已经跑完过一次）直接抛 ValueError——同一个理由，非
    None 的 input 会被当成新的一次运行，而 `results` 这个 operator.add 累加 channel 不会在
    同线程的新运行之间重置，静默产生翻倍的重复结果，比抛错更危险。这条判断只需要写在 emit
    有无两个分支共用的那一处（下面的 inputs 计算，在 ainvoke/astream 分支之前），不要分别
    加在两个分支里——astream 是本函数较晚加的分支，此前独立重犯过一次同一类错误。

    事件只带节点名这样的小摘要，不带 payload——与"大对象永远只用 ArtifactRef 引用"是同一条
    规则在事件流上的落点。事件命名沿用 paper_scout 立的 <module>/started|step|completed
    约定。

    Example:
        >>> results, ranking = await run_graph(problem, deps=deps, emit=emit)  # doctest: +SKIP
    """
    if (checkpointer is None) != (thread_id is None):
        raise ValueError("checkpointer and thread_id must be supplied together")

    # emit 塞进 deps 而不是单独往下传：候选子图内部节点（screen/novelty/.../regate）都
    # 只接收 CandidateState，唯一能把 emit 带到子图里的路径是 context_schema；candidate_node
    # 读 deps.emit 决定候选段是走 ainvoke（emit is None，行为不变）还是 astream（发候选级
    # 细粒度事件，任务四）。用 dataclasses.replace 而不是直接改 deps：deps 是
    # frozen dataclass，且调用方可能复用同一个 deps 实例跑多次，不该被这次调用的 emit 污染。
    deps = dataclasses.replace(deps, emit=emit) if emit is not None else deps

    compiled = build_compiled_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}} if thread_id else None
    inputs = {"problem": problem, "sample_size": sample_size}
    if config is not None:
        state = await compiled.aget_state(config)
        if state.next:
            inputs = None
        elif state.values:
            # 这条 thread 已经跑完过一次——非 None 的 input 会被当成在同一条 thread 上
            # 开一次新的运行，但 results 这个 operator.add 累加 channel 不会在同线程的
            # 新运行之间重置，会悄悄把新一轮的结果叠加到旧结果上（已实测：候选列表翻倍）。
            # 没有支持"在同一 thread 上重新开始"的用例，直接拒绝，比静默产出错误结果安全。
            raise ValueError(
                f"thread_id {thread_id!r} already has a completed run on this checkpointer; "
                "run_graph does not support re-running a finished thread"
            )

    if emit is None:
        final = await compiled.ainvoke(inputs, config, context=deps)
        return final["ordered_results"], final["ranking"]

    await emit("idea_generation/started", f"ev:{thread_id or 'run'}:started", None)
    final: dict = {}
    async for mode, chunk in compiled.astream(
        inputs, config, context=deps, stream_mode=["updates", "values"],
    ):
        if mode == "updates":
            for node_name in chunk:
                await emit("idea_generation/step", f"ev:{node_name}", {"node": node_name})
        else:
            final = chunk
    await emit("idea_generation/completed", f"ev:{thread_id or 'run'}:completed", None)
    return final["ordered_results"], final["ranking"]

"""Idea Generation 的编排层：整条链路的唯一入口都在这一个文件里。

对外两个入口：
- ``run_pre_gate``（P0 遗留）：单策略生成 -> structural_check -> falsifiability_check ->
  pre_gate，只跑到步骤 [4]。保留它是因为 ``demo_pre_gate.py`` 与既有测试仍以它作为最小闭环
  演示入口；P1+P2 的正式路径是 ``run_full_pipeline``。
- ``run_full_pipeline``（P1+P2）：空白挖掘 [2] -> 多候选生成+去重 [3] -> pre_gate [4] ->
  数值性审计 [5] -> 反方审阅 [6] -> 验证方案 [7] -> hard_gate [8] -> Elo 排序 [9]。

编排原则（延续此前 review 意见"整条链路应该是一个整体"）：生成/审计/门控不散落到多处调用
点，而是集中在一个函数里顺序 await；原先用 langgraph StateGraph 承载，依赖栈切到
openai + pydantic-ai 后改为纯异步顺序调用，承载手段变了但"一个整体"的约束不变。

Agent 的使用边界遵循 Occam's razor：只有真正需要工具权限+多轮检索的步骤 [2]/[5] 走
``core.agent.Agent``（见 evidence_retrieval.py）；[3]/[6]/[9] 这些单次结构化生成用
``single_turn_chat``，不接 ThreadManager/BaseAgent。
"""

import uuid

from pydantic_ai.models import Model

from athena.core.agent import Agent
from athena.core.schemas import ArtifactRef, Hypothesis
from athena.research.ranking import HypoPriList, PairwiseComparison, RankedCandidate
from athena.storage.artifact_store import ArtifactStore
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    IDEA_GENERATOR_SYSTEM_PROMPT,
    IDEA_GENERATOR_USER_PROMPT_TEMPLATE,
    PAIRWISE_JUDGE_SYSTEM_PROMPT,
    PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.candidate_generation import (
    MAX_VERBALIZED_SAMPLES,
    deduplicate_candidates,
    generate_candidates,
)
from athena.workflows.search.evidence_retrieval import collect_novelty_evidence, mine_research_gaps
from athena.workflows.search.gatekeeper import hard_gate, pre_gate
from athena.workflows.search.idea_schemas import (
    GateDecision,
    GateVerdict,
    HypothesisDraft,
    HypothesisPackage,
    PairwiseJudgment,
    PipelineCandidateResult,
    ResearchProblemInput,
)
from athena.workflows.search.pre_gate_checks import falsifiability_check, structural_check
from athena.workflows.search.review_and_validation import match_verifier, plan_validation, skeptic_review


# ====== 常量 ======

GENERATION_STRATEGY: str = "single_strategy_v1"
MAX_GENERATION_ATTEMPTS: int = 2


# ====== 对外入口 ======

async def run_pre_gate(
    problem: ResearchProblemInput, *, model: Model | str | None = None
) -> tuple[Hypothesis, HypothesisPackage, GateDecision]:
    """跑一遍 [3]→[4] 的 P0 闭环，返回 (Hypothesis, HypothesisPackage, GateDecision)。

    设计参考：生成阶段本身不做自我批判/自我打分（打分权始终在 gatekeeper.pre_gate 一处），
    呼应 Co-Scientist 论文里"生成与审阅分离，生成侧不自证"的思路（AI co-scientist,
    arXiv:2502.18864）；P0 只跑单一策略，多策略并行生成/空白挖掘留给后续迭代。

    Example:
        >>> node, package, decision = await run_pre_gate(problem, model=fake_model)  # doctest: +SKIP
        >>> decision.gate_phase
        'pre_gate'
    """
    idea_id = f"idea-{uuid.uuid4().hex[:12]}"

    evidence_lines = "\n".join(
        f"ev-{index}: {text}" for index, text in enumerate(problem.evidence_texts)
    ) or "(no evidence supplied)"
    constraint_lines = "\n".join(f"- {c}" for c in problem.constraints) or "(none)"
    user_prompt = IDEA_GENERATOR_USER_PROMPT_TEMPLATE.format(
        question=problem.question,
        domain=problem.domain,
        objective=problem.objective,
        constraints=constraint_lines,
        evidence=evidence_lines,
    )
    prompt = f"{IDEA_GENERATOR_SYSTEM_PROMPT}\n\n{user_prompt}"

    draft: HypothesisDraft | None = None
    last_error: Exception | None = None
    for _ in range(MAX_GENERATION_ATTEMPTS):
        try:
            draft = await single_turn_chat(prompt, HypothesisDraft, model=model)
            break
        except Exception as error:  # noqa: BLE001 - provider 报错形态不定，统一重试一次后再上抛
            last_error = error
    if draft is None:
        raise ValueError(f"IdeaGenerator failed to produce a valid HypothesisDraft: {last_error}")

    # 共享 schema 的 Hypothesis 没有 node_id/package_ref 这类身份字段了（RecordNode 已被移除），
    # 只承载内容；idea_id 只在本模块自己的 HypothesisPackage 里作为审计/关联用的标识
    node = Hypothesis(
        statement=draft.statement,
        intervention=draft.intervention,
        expected_effect=draft.expected_effect,
        evidence_refs=[ref for premise in draft.supported_premises for ref in premise.supporting_refs],
    )
    # 补上代码负责的 idea_id/lineage_op/validation_plan_ref；这些字段不该由 LLM 决定
    package = HypothesisPackage(
        idea_id=idea_id,
        generation_strategy=draft.generation_strategy or GENERATION_STRATEGY,
        novel_hypothesis=draft.statement,
        supported_premises=draft.supported_premises,
        inference_chain=draft.inference_chain,
        predicted_observations=draft.predicted_observations,
        disconfirming_observations=draft.disconfirming_observations,
        validation_plan_ref=None,
        lineage_op="generate",
    )

    structural_report = structural_check(package)
    falsifiability_report = await falsifiability_check(package, model=model)
    decision = pre_gate(structural_report, falsifiability_report)
    return node, package, decision


# ====== PairwiseJudge（HypoPriList 排序用比较器，步骤 [9]） ======

async def pairwise_compare(
    package_a: HypothesisPackage, package_b: HypothesisPackage, *, model: Model | str | None = None,
) -> PairwiseComparison:
    """轻量 PairwiseJudge：匿名化两个候选、双向各跑一次，规避 position/verbosity/
    self-preference 偏见（呼应设计文档第5节引用的 LLM-as-judge 偏见研究）。双向结果一致时
    直接采信；不一致时以正向结果为准，但把分歧写进 rationale 供审计。

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
    forward = await single_turn_chat(forward_prompt, PairwiseJudgment, model=model)
    backward = await single_turn_chat(backward_prompt, PairwiseJudgment, model=model)

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


# ====== 全流程编排（P1+P2） ======

async def run_full_pipeline(
    problem: ResearchProblemInput,
    *,
    gap_miner_agent: Agent,
    novelty_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    sample_size: int = MAX_VERBALIZED_SAMPLES,
    model: Model | str | None = None,
) -> tuple[list[PipelineCandidateResult], list[RankedCandidate]]:
    """P1+P2 全流程：[2]空白挖掘 → [3]多候选生成+去重 → 每个候选跑
    [4]pre_gate(不合格直接筛掉,跳过[5]-[8]) → [5]数值性审计 → [6]反方审阅 → [7]验证方案 →
    [8]hard_gate → [9]存活候选(PASS/EXPLORATORY) pairwise Elo 排序。

    gap_miner_agent 与 novelty_agent 是两个独立的 core.agent.Agent 实例(各自配了不同的
    system_prompt与各自的 agent.config.tools),分别用 evidence_retrieval.build_gap_miner_agent
    与 build_novelty_agent 构造,以保证绑定的是 GAP_MINER_SYSTEM_PROMPT/NOVELTY_SYSTEM_PROMPT；
    ResearchTree 交接(add_hypothesis)不在本轮范围内。

    Example:
        >>> results, ranking = await run_full_pipeline(problem, gap_miner_agent=agent1,
        ...     novelty_agent=agent2, artifacts=store, corpus_ref=corpus_ref,
        ...     model=fake_model)  # doctest: +SKIP
    """
    gaps = await mine_research_gaps(
        problem, agent=gap_miner_agent, corpus_ref=corpus_ref, artifacts=artifacts, model=model,
    )
    candidates = await generate_candidates(problem, gaps, sample_size=sample_size, model=model)
    candidates = deduplicate_candidates(candidates)

    results: list[PipelineCandidateResult] = []
    for package in candidates:
        structural = structural_check(package)
        falsifiability = await falsifiability_check(package, model=model)
        decision = pre_gate(structural, falsifiability)

        if decision.verdict != GateVerdict.PASS:
            results.append(PipelineCandidateResult(
                package=package, structural=structural, falsifiability=falsifiability, decision=decision,
            ))
            continue

        novelty = await collect_novelty_evidence(
            package, agent=novelty_agent, artifacts=artifacts, corpus_ref=corpus_ref, model=model,
        )
        skeptic = await skeptic_review(package, model=model)
        verifier = match_verifier(package, problem.domain)
        validation_plan = await plan_validation(package, verifier, artifacts=artifacts)
        # validation_plan_ref 必须指向 ValidationPlan 本身；早期实现误用了
        # estimated_cost_ref（那只是一小段成本估计 JSON），解析出来拿不到方案内容
        plan_ref = await artifacts.put_text(validation_plan.model_dump_json())
        package = package.model_copy(update={"validation_plan_ref": plan_ref})
        decision = hard_gate(structural, falsifiability, novelty, skeptic, validation_plan)

        results.append(PipelineCandidateResult(
            package=package, structural=structural, falsifiability=falsifiability,
            novelty=novelty, skeptic=skeptic, validation_plan=validation_plan, decision=decision,
        ))

    survivors = [r for r in results if r.decision.verdict in (GateVerdict.PASS, GateVerdict.EXPLORATORY)]
    book = HypoPriList()
    for survivor in survivors:
        book.ensure_registered(survivor.package.idea_id)
    for i in range(len(survivors)):
        for j in range(i + 1, len(survivors)):
            comparison = await pairwise_compare(survivors[i].package, survivors[j].package, model=model)
            book.record_comparison(comparison)

    return results, book.rank()

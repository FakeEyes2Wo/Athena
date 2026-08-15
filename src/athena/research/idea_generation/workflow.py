"""Idea Generation 的编排层：整条链路的唯一入口都在这一个文件里。

对外入口 ``run_full_pipeline``：空白挖掘 [2] -> 多候选生成+去重 [3] -> 每个候选跑
[4]pre_gate（不合格直接筛掉，跳过 [5]-[8]） -> [5]数值性审计 -> [6]三视角审阅委员会
-> [7]验证方案 -> [8]hard_gate（REVISE 时跑 [8']修订闭环 -> 终局刷新 -> 终审）
-> 存活候选（PASS/EXPLORATORY）pairwise Elo 排序 [9]。

编排是纯 asyncio（gather + Semaphore），不是 langgraph StateGraph——这条分支没有引入
langgraph 依赖，且时间紧张不适合新增依赖；行为对齐旧分支迁移前（P0-P2）的纯异步顺序调用
版本，只是把"每个候选一条协程"通过 asyncio.gather 并发起来。
"""

import asyncio

from athena.core.agent import Agent
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.research.idea_generation.candidate_generation import (
    MAX_VERBALIZED_SAMPLES,
    deduplicate_candidates,
    generate_candidates,
)
from athena.research.idea_generation.evidence_retrieval import (
    collect_novelty_evidence,
    degraded_novelty_report,
    mine_research_gaps,
)
from athena.research.idea_generation.gatekeeper import hard_gate, pre_gate
from athena.research.idea_generation.idea_schemas import (
    GateVerdict,
    HypothesisPackage,
    PairwiseJudgment,
    PipelineCandidateResult,
    ResearchProblemInput,
)
from athena.research.idea_generation.pre_gate_checks import (
    degraded_falsifiability_report,
    falsifiability_check,
    structural_check,
)
from athena.research.idea_generation.ranking import HypoPriList, PairwiseComparison, RankedCandidate
from athena.research.idea_generation.review_board import review_board
from athena.research.idea_generation.revision import is_revisable, refresh_stale_evidence, run_debate
from athena.research.idea_generation.prompts import (
    PAIRWISE_JUDGE_SYSTEM_PROMPT,
    PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE,
)
from athena.research.idea_generation.state import PipelineDeps
from athena.research.idea_generation.structured_chat import single_turn_structured_chat
from athena.research.idea_generation.validation import match_verifier, plan_validation


async def _process_one_candidate(
    package: HypothesisPackage, problem: ResearchProblemInput, *, deps: PipelineDeps,
) -> PipelineCandidateResult:
    """跑完一个候选的 [4]-[8]（含修订闭环）。

    Example:
        >>> result = await _process_one_candidate(package, problem, deps=deps)  # doctest: +SKIP
    """
    async with deps.llm_sem:
        structural = structural_check(package)
        try:
            falsifiability = await falsifiability_check(
                package, model=deps.model, artifacts=deps.artifacts,
            )
        except Exception as error:  # noqa: BLE001 - provider 报错形态不定，统一降级
            falsifiability = degraded_falsifiability_report(package.idea_id, error)
    decision = pre_gate(structural, falsifiability)

    if decision.verdict != GateVerdict.PASS:
        return PipelineCandidateResult(
            package=package, structural=structural, falsifiability=falsifiability,
            novelty=None, reviews=[], validation_plan=None, decision=decision,
        )

    try:
        novelty = await collect_novelty_evidence(
            package, agent=deps.novelty_agent, artifacts=deps.artifacts,
            corpus_ref=deps.corpus_ref, llm_sem=deps.llm_sem, retrieval_sem=deps.retrieval_sem,
            model=deps.model,
        )
    except Exception as error:  # noqa: BLE001 - 检索失败降级为空报告，不拖垮这个候选
        novelty = await degraded_novelty_report(package.idea_id, error, deps.artifacts)

    reviews = await review_board(
        package, novelty, domain_review_agent=deps.domain_review_agent, artifacts=deps.artifacts,
        corpus_ref=deps.corpus_ref, llm_sem=deps.llm_sem, retrieval_sem=deps.retrieval_sem,
        model=deps.model,
    )

    verifier = match_verifier(package, problem.domain)
    validation_plan = await plan_validation(package, verifier, artifacts=deps.artifacts)
    plan_ref = await deps.artifacts.put_text(validation_plan.model_dump_json())
    package = package.model_copy(update={"validation_plan_ref": plan_ref})

    decision = hard_gate(structural, falsifiability, novelty, reviews, validation_plan)
    revisions: list = []
    revision_blocking_factor: str | None = None

    if is_revisable(decision):
        revision_blocking_factor = decision.blocking_factor
        prior_transcript = await deps.artifacts.get_text(novelty.query_log_ref) if novelty.query_log_ref else ""
        package, reviews, revisions = await run_debate(
            package, blocking_factor=decision.blocking_factor, reviews=reviews,
            artifacts=deps.artifacts, corpus_ref=deps.corpus_ref,
            prior_transcript=prior_transcript, llm_sem=deps.llm_sem, model=deps.model,
        )
        package, structural, falsifiability, novelty, reviews, validation_plan = (
            await refresh_stale_evidence(
                package, problem_domain=problem.domain, novelty=novelty, reviews=reviews,
                novelty_agent=deps.novelty_agent, domain_review_agent=deps.domain_review_agent,
                artifacts=deps.artifacts, corpus_ref=deps.corpus_ref,
                llm_sem=deps.llm_sem, retrieval_sem=deps.retrieval_sem, model=deps.model,
            )
        )
        decision = hard_gate(structural, falsifiability, novelty, reviews, validation_plan)

    return PipelineCandidateResult(
        package=package, structural=structural, falsifiability=falsifiability,
        novelty=novelty, reviews=reviews, validation_plan=validation_plan, decision=decision,
        revisions=revisions, revision_blocking_factor=revision_blocking_factor,
    )


async def _pairwise_compare(
    package_a: HypothesisPackage, package_b: HypothesisPackage, *,
    llm_sem: asyncio.Semaphore, artifacts: ArtifactStore, model: str,
) -> PairwiseComparison:
    """匿名化两个候选、双向各跑一次，规避 position/verbosity/self-preference 偏见。

    Example:
        >>> comparison = await _pairwise_compare(pkg_a, pkg_b, llm_sem=sem,
        ...     artifacts=store, model="m")  # doctest: +SKIP
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
        async with llm_sem:
            return await single_turn_structured_chat(
                prompt, PairwiseJudgment, model=model, artifacts=artifacts,
            )

    forward, backward = await asyncio.gather(_run(forward_prompt), _run(backward_prompt))

    forward_winner = package_a.idea_id if forward.winner == "candidate_a" else package_b.idea_id
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


async def run_full_pipeline(
    problem: ResearchProblemInput,
    *,
    gap_miner_agent: Agent,
    novelty_agent: Agent,
    domain_review_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    sample_size: int = MAX_VERBALIZED_SAMPLES,
    model: str,
    llm_concurrency: int = 16,
    retrieval_concurrency: int = 4,
) -> tuple[list[PipelineCandidateResult], list[RankedCandidate]]:
    """P1+P2 全流程：[2]空白挖掘 -> [3]多候选生成+去重 -> 每个候选跑
    [4]pre_gate(不合格直接筛掉,跳过[5]-[8]) -> [5]数值性审计 -> [6]三视角审阅委员会 ->
    [7]验证方案 -> [8]hard_gate（REVISE 时修订闭环）-> 存活候选(PASS/EXPLORATORY)
    pairwise Elo 排序 [9]。

    Example:
        >>> results, ranking = await run_full_pipeline(problem, gap_miner_agent=a1,
        ...     novelty_agent=a2, domain_review_agent=a3, artifacts=store,
        ...     corpus_ref=corpus_ref, model="m")  # doctest: +SKIP
    """
    deps = PipelineDeps(
        artifacts=artifacts, corpus_ref=corpus_ref,
        llm_sem=asyncio.Semaphore(llm_concurrency), retrieval_sem=asyncio.Semaphore(retrieval_concurrency),
        gap_miner_agent=gap_miner_agent, novelty_agent=novelty_agent,
        domain_review_agent=domain_review_agent, model=model,
    )

    gaps = await mine_research_gaps(
        problem, agent=gap_miner_agent, corpus_ref=corpus_ref, artifacts=artifacts, model=model,
    )
    candidates = deduplicate_candidates(
        await generate_candidates(problem, gaps, artifacts=artifacts, sample_size=sample_size, model=model)
    )
    if not candidates:
        return [], []

    results = await asyncio.gather(*[
        _process_one_candidate(package, problem, deps=deps) for package in candidates
    ])

    survivors = [r for r in results if r.decision.verdict in (GateVerdict.PASS, GateVerdict.EXPLORATORY)]
    book = HypoPriList()
    for survivor in survivors:
        book.ensure_registered(survivor.package.idea_id)

    pairs = [(i, j) for i in range(len(survivors)) for j in range(i + 1, len(survivors))]
    comparisons = await asyncio.gather(*[
        _pairwise_compare(
            survivors[i].package, survivors[j].package,
            llm_sem=deps.llm_sem, artifacts=artifacts, model=model,
        )
        for i, j in pairs
    ], return_exceptions=True)
    for comparison in comparisons:
        if isinstance(comparison, PairwiseComparison):
            book.record_comparison(comparison)

    return results, book.rank()

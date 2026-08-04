"""Idea Generation 的编排层：整条链路的唯一入口都在这一个文件里。

对外两个入口：
- ``run_pre_gate``（P0 遗留）：单策略生成 -> structural_check -> falsifiability_check ->
  pre_gate，只跑到步骤 [4]。保留它是因为 ``demo_pre_gate.py`` 与既有测试仍以它作为最小闭环
  演示入口；P1+P2 的正式路径是 ``run_full_pipeline``。
- ``run_full_pipeline``（P1+P2）：空白挖掘 [2] -> 多候选生成+去重 [3] -> pre_gate [4] ->
  数值性审计 [5] -> 三视角审阅委员会 [6]（methodology/statistics/domain_consistency 并行）
  -> 验证方案 [7] -> hard_gate [8] -> 存活候选（PASS/EXPLORATORY）pairwise Elo 排序 [9]。

编排原则（延续此前 review 意见"整条链路应该是一个整体"）：生成/审计/门控不散落到多处调用
点，而是集中在一个函数里顺序 await；原先用 langgraph StateGraph 承载，依赖栈切到
openai + pydantic-ai 后改为纯异步顺序调用，承载手段变了但"一个整体"的约束不变。

Agent 的使用边界遵循 Occam's razor：只有真正需要工具权限+多轮检索的步骤才走
``core.agent.Agent``——[2] 空白挖掘、[5] 数值性审计（见 evidence_retrieval.py），以及 [6]
审阅委员会里唯一配了 paper_rag 工具、需要跑检索循环核对领域一致性的 domain_consistency
视角（见 review_board.py 的 ``build_domain_consistency_agent``/``review_one_perspective``）。
其余单次结构化生成——[3] 候选生成、[6] 里仅靠包内信息即可判定的 methodology/statistics
两个视角、以及 [9] pairwise 比较——都走 ``single_turn_chat``，不接 ThreadManager/BaseAgent。

并发编排（本分支的另一大动因）：``run_full_pipeline`` 内部按 LLM_CONCURRENCY /
RETRIEVAL_CONCURRENCY 建两级 ``asyncio.Semaphore``（下面这两个常量），分别约束
single_turn_chat 调用与带检索的 Agent 循环。候选之间、审阅委员会内的三个视角之间、以及
pairwise 双向调用之间全部用 ``asyncio.gather`` 并发发起，取代 P0 时代的顺序 for 循环；
``asyncio.gather`` 天然保序，各阶段之间靠这一点对齐候选顺序，而不是另建索引结构。名额获取
与释放的粒度、以及死锁规避规则见两个常量与 ``_audit_candidate``/``pairwise_compare`` 各自
的函数注释。
"""

import asyncio
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
from athena.workflows.search.evidence_retrieval import (
    collect_novelty_evidence,
    degraded_novelty_report,
    limited_by,
    mine_research_gaps,
)
from athena.workflows.search.gatekeeper import hard_gate, pre_gate
from athena.workflows.search.idea_schemas import (
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    HypothesisDraft,
    HypothesisPackage,
    PairwiseJudgment,
    PipelineCandidateResult,
    ResearchProblemInput,
    StructuralCheckReport,
)
from athena.workflows.search.pre_gate_checks import (
    degraded_falsifiability_report,
    falsifiability_check,
    structural_check,
)
from athena.workflows.search.review_board import read_prior_transcript, review_board
from athena.workflows.search.revision import is_revisable, refresh_stale_evidence, run_debate
from athena.workflows.search.validation import match_verifier, plan_validation


# ====== 常量 ======

GENERATION_STRATEGY: str = "single_strategy_v1"
MAX_GENERATION_ATTEMPTS: int = 2

LLM_CONCURRENCY: int = 16
"""single_turn_chat 的并发上限。

按 **pairwise 阶段**定的——那是全流水线唯一有确定并发上界的阶段：n 个存活候选 = C(n,2) 对,
每对双向 2 次且并发,5 个候选就是 20 次同时在飞。审阅阶段峰值与之同量级(3 个视角调用 +
novelty 与 domain_consistency 各自的转结构化调用),但随视角数与候选数两个变量浮动,不适合
作为定值依据。日后调 MAX_VERBALIZED_SAMPLES 或增删视角时,该动的是这条注释描述的推算,
不是随手改数字。"""

RETRIEVAL_CONCURRENCY: int = 4
"""带 paper_rag 工具的 Agent 检索循环的并发上限。保守估计,未经 paper_rag 实际承受能力验证。

**名额必须在单个检索循环的粒度上获取与释放**,禁止跨越 novelty -> domain_consistency 的
依赖边界持有:持有一个名额的同时等待第二个名额,在候选数 >= 名额数时必然死锁(5 个候选抢
4 个名额,4 个各持 1 个、各等 1 个)。只用 async with 获取,不手动 acquire()/release()——
异常路径漏 release 会静默泄漏名额,症状是流水线越跑越慢直至卡死,极难定位。"""


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
    package_a: HypothesisPackage, package_b: HypothesisPackage, *,
    llm_sem: asyncio.Semaphore | None = None, model: Model | str | None = None,
) -> PairwiseComparison:
    """轻量 PairwiseJudge：匿名化两个候选、双向各跑一次，规避 position/verbosity/
    self-preference 偏见（呼应设计文档第5节引用的 LLM-as-judge 偏见研究）。双向结果一致时
    直接采信；不一致时以正向结果为准，但把分歧写进 rationale 供审计。

    forward/backward 之间没有依赖，用 gather 并发发起；两次调用各自在自己的协程内部
    ``async with limited_by(llm_sem)`` 获取名额，不在外层整体持有一个名额再等第二个——
    那是候选数 >= 名额数时必然死锁的模式（见 evidence_retrieval.limited_by 的注释）。

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

    # 注：设计文档 §6.2 要求所有并发点都用 asyncio.gather(..., return_exceptions=True)，
    # 这里刻意没加——run_full_pipeline 里包这一层调用的外层 gather 已经带
    # return_exceptions=True（见下方 pairwise 阶段），forward/backward 任一失败都会在这里
    # 直接向外抛，交给外层兜底跳过整对比较；在这里再吞一次异常只会让外层的降级逻辑看不到
    # 失败发生在哪一侧，没有实际收益。
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


# ====== 候选内子编排（供 run_full_pipeline 并发调度） ======

async def _screen_candidate(
    package: HypothesisPackage,
    *,
    llm_sem: asyncio.Semaphore,
    model: Model | str | None = None,
) -> tuple[StructuralCheckReport, FalsifiabilityReport, GateDecision]:
    """步骤 [4]：结构检查 + 可证伪性审计 + pre_gate。廉价前置筛子，跑在昂贵步骤之前。

    falsifiability_check 失败按不可证伪处理而非上抛：没有证据不能算通过，交给 pre_gate
    判 REVISE，不静默放行也不拖垮整批候选。

    Example:
        >>> structural, falsifiability, decision = await _screen_candidate(
        ...     package, llm_sem=sem)  # doctest: +SKIP
    """
    structural = structural_check(package)
    try:
        async with limited_by(llm_sem):
            falsifiability = await falsifiability_check(package, model=model)
    except Exception as error:  # noqa: BLE001 - provider 报错形态不定，一律降级
        # 审计没跑成不能算"可证伪"——没有证据不能算通过，交给 pre_gate 判 REVISE
        falsifiability = degraded_falsifiability_report(package.idea_id, error)
    return structural, falsifiability, pre_gate(structural, falsifiability)


async def _audit_candidate(
    package: HypothesisPackage,
    problem: ResearchProblemInput,
    structural: StructuralCheckReport,
    falsifiability: FalsifiabilityReport,
    *,
    novelty_agent: Agent,
    domain_review_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    llm_sem: asyncio.Semaphore,
    retrieval_sem: asyncio.Semaphore,
    model: Model | str | None = None,
) -> PipelineCandidateResult:
    """步骤 [5]-[8] 加修订闭环：novelty -> review_board -> 验证方案 -> hard_gate ->
    （若判 REVISE 且可修订）辩论 -> 终局刷新 -> 终审。

    structural / falsifiability 由 _screen_candidate 传入而非重算，避免每个候选多一次
    LLM 调用。novelty 与 review_board 之间是本设计唯一的候选内串行依赖（domain_consistency
    复用 novelty 的检索转录）；retrieval_sem 的名额在各自的检索循环内部获取与释放，绝不跨
    这条依赖边界持有——否则候选数 >= 名额数时必然死锁。

    collect_novelty_evidence 失败降级为空报告而不是上抛，不拖垮整批候选：facet_overlap
    为空会命中 hard_gate 既有的"空 facet 判 REVISE"逻辑，无需另写判定；query_log_ref=None
    让 domain_consistency 退回完整检索，不被 novelty 的失败牵连——一次检索抖动不该同时
    废掉两项 rubric。

    修订闭环只在 hard_gate 判 REVISE 且 is_revisable 之上触发：run_debate 本身不跑检索
    （对手对修订稿重表态只是一次 single_turn_chat），refresh_stale_evidence 是这条闭环里
    唯一可能重新触发检索的地方,且内部保证"先 novelty、再视角"的刷新顺序。

    是否重跑 refresh_stale_evidence + 终审第二次 hard_gate，判据是 package.revision_round
    有没有比进入辩论前更大，**不是** rounds 是否非空：reviser 直接失败时 rounds 恒为空，
    revision_round 自然不变；但"第一轮就是空转 no-op"这条分支会向 rounds 追加一条
    cleared=False 的审计记录（no-op 本身值得留痕），revision_round 却和进入前一样，两者不
    等价。用 rounds 非空当判据会让 no-op 场景误触发 refresh_stale_evidence 里那个无条件的
    falsifiability_check（真实 LLM 调用）与第二次 hard_gate——花了钱、还给一个从未被真正
    修订过的候选贴上 revision_blocking_factor,审计记录变得自相矛盾。用 revision_round 判据
    时，no-op 场景下候选原样返回、只多一条 rounds 记录，不重跑任何东西、也不再调用
    hard_gate 第二次；revision_blocking_factor 同理只在真的发生过修订时才写，否则留 None。
    hard_gate 在真正发生修订时恰好调用两次：进入修订闭环前一次、闭环跑完后终审一次；
    revision.py 全程不产出 verdict。

    Example:
        >>> result = await _audit_candidate(package, problem, structural, falsifiability,
        ...     novelty_agent=a1, domain_review_agent=a2, artifacts=store,
        ...     corpus_ref=ref, llm_sem=s1, retrieval_sem=s2)  # doctest: +SKIP
    """
    try:
        novelty = await collect_novelty_evidence(
            package, agent=novelty_agent, artifacts=artifacts, corpus_ref=corpus_ref,
            llm_sem=llm_sem, retrieval_sem=retrieval_sem, model=model,
        )
    except Exception as error:  # noqa: BLE001 - 检索失败降级为空报告，不拖垮整批
        novelty = await degraded_novelty_report(package.idea_id, error, artifacts)
    reviews = await review_board(
        package, novelty, domain_review_agent=domain_review_agent, artifacts=artifacts,
        corpus_ref=corpus_ref, llm_sem=llm_sem, retrieval_sem=retrieval_sem, model=model,
    )
    verifier = match_verifier(package, problem.domain)
    validation_plan = await plan_validation(package, verifier, artifacts=artifacts)
    # validation_plan_ref 必须指向 ValidationPlan 本身，不是成本估计
    plan_ref = await artifacts.put_text(validation_plan.model_dump_json())
    package = package.model_copy(update={"validation_plan_ref": plan_ref})
    decision = hard_gate(structural, falsifiability, novelty, reviews, validation_plan)
    if not is_revisable(decision):
        return PipelineCandidateResult(
            package=package, structural=structural, falsifiability=falsifiability,
            novelty=novelty, reviews=reviews, validation_plan=validation_plan,
            decision=decision,
        )

    # 修订闭环：辩论（不跑检索）→ 终局刷新（唯一可能产生检索的地方）→ 终审
    blocking_factor = decision.blocking_factor
    prior_transcript = await read_prior_transcript(novelty, artifacts)
    pre_debate_revision_round = package.revision_round
    package, reviews, rounds = await run_debate(
        package, blocking_factor=blocking_factor, reviews=reviews, artifacts=artifacts,
        corpus_ref=corpus_ref, prior_transcript=prior_transcript, llm_sem=llm_sem, model=model,
    )
    # 判据是 package 有没有真的被修订过（revision_round 前进了），不是 rounds 是否非空——
    # no-op 分支也会往 rounds 里追加一条审计记录，但 revision_round 原地不动，见函数注释。
    revised = package.revision_round > pre_debate_revision_round
    if revised:
        package, structural, falsifiability, novelty, reviews, validation_plan = (
            await refresh_stale_evidence(
                package, problem_domain=problem.domain, novelty=novelty, reviews=reviews,
                novelty_agent=novelty_agent, domain_review_agent=domain_review_agent,
                artifacts=artifacts, corpus_ref=corpus_ref, llm_sem=llm_sem,
                retrieval_sem=retrieval_sem, model=model,
            )
        )
        decision = hard_gate(structural, falsifiability, novelty, reviews, validation_plan)
    return PipelineCandidateResult(
        package=package, structural=structural, falsifiability=falsifiability,
        novelty=novelty, reviews=reviews, validation_plan=validation_plan, decision=decision,
        revisions=rounds, revision_blocking_factor=blocking_factor if revised else None,
    )


# ====== 全流程编排（P1+P2） ======

async def run_full_pipeline(
    problem: ResearchProblemInput,
    *,
    gap_miner_agent: Agent,
    novelty_agent: Agent,
    domain_review_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    sample_size: int = MAX_VERBALIZED_SAMPLES,
    model: Model | str | None = None,
) -> tuple[list[PipelineCandidateResult], list[RankedCandidate]]:
    """P1+P2 全流程：[2]空白挖掘 → [3]多候选生成+去重 → 每个候选跑
    [4]pre_gate(不合格直接筛掉,跳过[5]-[8]) → [5]数值性审计 → [6]三视角审阅委员会 →
    [7]验证方案 → [8]hard_gate → [9]存活候选(PASS/EXPLORATORY) pairwise Elo 排序。

    gap_miner_agent 与 novelty_agent 是两个独立的 core.agent.Agent 实例(各自配了不同的
    system_prompt与各自的 agent.config.tools),分别用 evidence_retrieval.build_gap_miner_agent
    与 build_novelty_agent 构造,以保证绑定的是 GAP_MINER_SYSTEM_PROMPT/NOVELTY_SYSTEM_PROMPT；
    domain_review_agent 同理由 review_board.build_domain_consistency_agent 构造,供 [6] 的
    domain_consistency 视角使用；ResearchTree 交接(add_hypothesis)不在本轮范围内。

    Example:
        >>> results, ranking = await run_full_pipeline(problem, gap_miner_agent=agent1,
        ...     novelty_agent=agent2, domain_review_agent=agent3, artifacts=store,
        ...     corpus_ref=corpus_ref, model=fake_model)  # doctest: +SKIP
    """
    gaps = await mine_research_gaps(
        problem, agent=gap_miner_agent, corpus_ref=corpus_ref, artifacts=artifacts, model=model,
    )
    candidates = await generate_candidates(problem, gaps, sample_size=sample_size, model=model)
    candidates = deduplicate_candidates(candidates)

    llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)
    retrieval_sem = asyncio.Semaphore(RETRIEVAL_CONCURRENCY)

    # 阶段一：pre_gate 全并发。gather 保序，pre_gated[i] 对应 candidates[i]
    # 注：设计文档 §6.2 要求所有并发点都用 return_exceptions=True，这里刻意没加——
    # _screen_candidate 内部已经把 falsifiability_check 的失败降级为空报告（见其注释），
    # 正常路径下不会再有异常穿透到这一层；如果确实穿透了，说明出现了未预期的 bug，
    # fail-fast 中止比悄悄吞掉、产出一批部分结果更安全。
    pre_gated = await asyncio.gather(*[
        _screen_candidate(package, llm_sem=llm_sem, model=model) for package in candidates
    ])

    # 阶段二：只有 PASS 的候选进昂贵段，同样保序
    # 注：同上，设计文档 §6.2 要求 return_exceptions=True，这里刻意没加。_audit_candidate
    # 内部只对 collect_novelty_evidence 做了失败降级；artifacts.put_text / plan_validation /
    # hard_gate 没有类似保护。ArtifactStore 一次 I/O 失败被当成环境故障而非某个候选自身的
    # 问题——"重试/跳过单个候选"对存储层故障没有意义，所以任其向外抛出、让整轮 fail-fast
    # 更符合语义。代价是异常抛出瞬间姐妹协程不会被取消，会成为孤儿继续持有
    # llm_sem/retrieval_sem 名额直至自然结束；把"候选级失败"定义成不拖垮整批的结果形态
    # 属于新设计，不在本轮范围内。
    survivor_indices = [i for i, (_, _, d) in enumerate(pre_gated) if d.verdict == GateVerdict.PASS]
    audited = await asyncio.gather(*[
        _audit_candidate(
            candidates[i], problem, pre_gated[i][0], pre_gated[i][1],
            novelty_agent=novelty_agent, domain_review_agent=domain_review_agent,
            artifacts=artifacts, corpus_ref=corpus_ref,
            llm_sem=llm_sem, retrieval_sem=retrieval_sem, model=model,
        )
        for i in survivor_indices
    ])

    # 阶段三：按 candidates 输入序组装 results
    audited_by_index = dict(zip(survivor_indices, audited))
    results: list[PipelineCandidateResult] = []
    for i, (structural, falsifiability, decision) in enumerate(pre_gated):
        if i not in audited_by_index:
            results.append(PipelineCandidateResult(
                package=candidates[i], structural=structural,
                falsifiability=falsifiability, decision=decision,
            ))
            continue
        results.append(audited_by_index[i])

    survivors = [r for r in results if r.decision.verdict in (GateVerdict.PASS, GateVerdict.EXPLORATORY)]
    book = HypoPriList()
    for survivor in survivors:
        book.ensure_registered(survivor.package.idea_id)

    pairs = [(i, j) for i in range(len(survivors)) for j in range(i + 1, len(survivors))]
    comparisons = await asyncio.gather(*[
        pairwise_compare(survivors[i].package, survivors[j].package, llm_sem=llm_sem, model=model)
        for i, j in pairs
    ], return_exceptions=True)

    # 比较可以全并发执行，但必须按 (i, j) 索引序喂入——Elo 是在线增量更新，顺序影响评分。
    # 而 (i, j) 有序的前提是 survivors 保持 results 的输入序，results 又保持 candidates 的
    # 输入序（asyncio.gather 天然保序，禁止改用 as_completed）。
    for comparison in comparisons:
        if isinstance(comparison, PairwiseComparison):
            book.record_comparison(comparison)

    return results, book.rank()

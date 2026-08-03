"""REVISE 修订闭环（步骤 [8] 之后）：被 hard_gate 判 REVISE 的候选与拦住它的那个审阅视角
进行有界辩论，修订后按逐报告输入指纹只重跑真正失效的证据，再交回 gatekeeper 终审。

三条贯穿本模块的约束：
- **不产出 verdict**。终止条件用 gatekeeper 已有的 rubric 项谓词 perspective_ok（返回 bool），
  判决权全程留在 hard_gate 手里，整个闭环只调用它两次（进入前、终审）。
- **辩论轮不跑检索**。domain_consistency 的检索转录已经冻结在 SkepticReport.transcript_ref
  里，对修订稿重新表态只需一次 single_turn_chat。检索只可能出现在终局 staleness 刷新里。
- **reviser 看不到 sampling_probability**。它写的 rebuttal 要送进审阅侧 prompt，看得见就可能
  经由答辩文本泄漏（Co-Scientist：审阅侧不得锚定生成侧自评）。prompt 逐字段拼装，禁止
  model_dump_json()。
"""

import asyncio

from pydantic_ai.models import Model

from athena.core.agent import Agent
from athena.core.schemas import ArtifactRef
from athena.storage.artifact_store import ArtifactStore
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    DEBATE_REREVIEW_PROMPT_TEMPLATE,
    DEBATE_REREVIEW_SYSTEM_PROMPT,
    REVISER_SYSTEM_PROMPT,
    REVISION_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.evidence_retrieval import (
    build_novelty_question,
    collect_novelty_evidence,
    degraded_novelty_report,
    limited_by,
)
from athena.workflows.search.gatekeeper import MAX_TOTAL_RISKS, perspective_ok
from athena.workflows.search.idea_schemas import (
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    HypothesisPackage,
    NoveltyEvidenceReport,
    RevisionDraft,
    RevisionRound,
    SkepticJudgment,
    SkepticReport,
    StructuralCheckReport,
    ValidationPlan,
)
from athena.workflows.search.pre_gate_checks import (
    degraded_falsifiability_report,
    falsifiability_check,
    structural_check,
)
from athena.workflows.search.review_board import (
    REVIEW_PERSPECTIVES,
    ReviewPerspective,
    build_perspective_input,
    format_premise_lines,
    read_prior_transcript,
    review_one_perspective,
)
from athena.workflows.search.validation import match_verifier, plan_validation


# ====== 常量 ======

MAX_DEBATE_ROUNDS: int = 2
"""辩论轮次上限。1 轮退化成单向修订——对手对修订稿的表态即终审，没有往返，不成其为辩论；
2 轮是"对手对修订稿提新意见 → reviser 再应"这一往返成立的最小轮数。单候选最坏 +4 次
single_turn_chat。**无经验依据**，与 MAX_TOLERATED_RISKS / MAX_TOTAL_RISKS 同属"先取保守
起点、真实跑过几轮后一并校准"。"""

MAX_REVISION_ATTEMPTS: int = 2
"""单次 reviser 调用的最大尝试次数（即重试 1 次）。沿用 MAX_GENERATION_ATTEMPTS /
MAX_REVIEW_ATTEMPTS 的先例。重试逻辑在核心流程跑通之后才接进辩论循环（见 Task 10）。"""


# ====== 入口条件与对手映射 ======

RISK_ITEM_PREFIX: str = "risk_ok_"
RISK_TOTAL_ITEM: str = "risk_total"


def is_revisable(decision: GateDecision) -> bool:
    """该判决是否有资格进修订闭环。

    只有 risk_ok_<p> 与 risk_total 可达：evidence_traceable / falsifiable 在
    run_full_pipeline 里永不作为入口出现（进昂贵段的前提就是 pre_gate 已判它们通过，
    hard_gate 拿同样两个报告对象重算结果必然相同）；novelty_ok 判 REVISE 只由 facet_overlap
    为空触发，那是检索失败而非候选缺陷，改假设文本不会让检索恢复；verifier_ok 判
    EXPLORATORY 不是 REVISE；REJECT 按定义不可由修订解决。

    Example:
        >>> is_revisable(decision)  # doctest: +SKIP
        True
    """
    if decision.verdict != GateVerdict.REVISE or decision.blocking_factor is None:
        return False
    return (decision.blocking_factor.startswith(RISK_ITEM_PREFIX)
            or decision.blocking_factor == RISK_TOTAL_ITEM)


def select_debate_opponent(blocking_factor: str, reviews: list[SkepticReport]) -> str:
    """被拦项 -> 辩论对手的 perspective_id。

    risk_ok_<p> 直接取后缀；risk_total 取 unaddressed_risks 最多的视角，并列时取
    REVIEW_PERSPECTIVES 中靠前者。**reviews 在函数内部按 REVIEW_PERSPECTIVES 重排**——把
    确定性寄托在调用方的入参顺序（asyncio.gather）上等于没有承诺，这与 hard_gate 内部重排
    是同一条理由。risk_total 分支里的 max() 不会遇到空序列：能走到这个分支之前，hard_gate
    已经强制要求每个视角恰好一份 review（否则直接抛错），所以 ordered 恒非空。

    Example:
        >>> select_debate_opponent("risk_ok_statistics", reviews)  # doctest: +SKIP
        'statistics'
    """
    if blocking_factor.startswith(RISK_ITEM_PREFIX):
        return blocking_factor[len(RISK_ITEM_PREFIX):]
    by_perspective = {r.perspective: r for r in reviews}
    ordered = [by_perspective[p.perspective_id] for p in REVIEW_PERSPECTIVES
               if p.perspective_id in by_perspective]
    # max 返回首个最大值，配合上面按 REVIEW_PERSPECTIVES 的重排即得到确定的并列裁决
    return max(ordered, key=lambda r: len(r.unaddressed_risks)).perspective


def is_no_op_revision(draft: RevisionDraft, package: HypothesisPackage) -> bool:
    """这一轮修订有没有实质改动。判定域是 RevisionDraft 的四个 revised_* 字段，逐字段相等
    比对——纯函数、无阈值、确定性可测。

    **不要改用 novel_hypothesis 的 Jaccard 相似度**：去重问的是"这是不是同一个 idea"，本函数
    问的是"这一轮有没有实质改动"，判定域完全不同。按前者判定，最典型的修订（只补
    disconfirming_observations，假设文本一字未动）相似度为 1.0，guard 第一轮就开火、对手
    一次都不会被叫到，主路径直接失效。

    Example:
        >>> is_no_op_revision(draft, package)  # doctest: +SKIP
        False
    """
    return (draft.revised_novel_hypothesis == package.novel_hypothesis
            and draft.revised_premises == package.supported_premises
            and draft.revised_predicted_observations == package.predicted_observations
            and draft.revised_disconfirming_observations == package.disconfirming_observations)


# ====== Reviser（生成侧：看得到 critique，看不到阈值与自评概率） ======

def build_revision_prompt(
    package: HypothesisPackage,
    *,
    blocking_factor: str,
    debated_perspective: str,
    reviews: list[SkepticReport],
    prior_rounds: list[str],
) -> str:
    """拼装 reviser 的 prompt。逐字段拼装，**绝不序列化整个 package**——model_dump_json()
    会一次性带出 sampling_probability，把三道泄漏防线同时废掉。

    刻意把另外两个视角的 critique 也给出来：它们的报告在辩论期间被冻结复用，reviser 看不见
    就只能瞎改，而改坏了要到终审才暴露、还会触发 staleness 重跑（最贵的那条路径）。

    Example:
        >>> "risk_ok_methodology" in build_revision_prompt(package,
        ...     blocking_factor="risk_ok_methodology", debated_perspective="methodology",
        ...     reviews=reviews, prior_rounds=[])  # doctest: +SKIP
        True
    """
    by_perspective = {r.perspective: r for r in reviews}
    blocking = by_perspective.get(debated_perspective)
    premise_lines = format_premise_lines(package)
    other_lines = "\n".join(
        f"- [{r.perspective}] {r.critique} | risks: {r.unaddressed_risks}"
        for r in reviews if r.perspective != debated_perspective
    ) or "(none)"

    user_prompt = REVISION_USER_PROMPT_TEMPLATE.format(
        blocking_factor=blocking_factor,
        debated_perspective=debated_perspective,
        blocking_critique=blocking.critique if blocking else "(unavailable)",
        blocking_risks="\n".join(f"- {r}" for r in blocking.unaddressed_risks)
                       if blocking and blocking.unaddressed_risks else "(none listed)",
        other_critiques=other_lines,
        novel_hypothesis=package.novel_hypothesis,
        supported_premises=premise_lines,
        predicted_observations="\n".join(f"- {o}" for o in package.predicted_observations),
        disconfirming_observations="\n".join(
            f"- {o}" for o in package.disconfirming_observations),
        prior_rounds="\n".join(f"- {r}" for r in prior_rounds) or "(this is the first round)",
    )
    return f"{REVISER_SYSTEM_PROMPT}\n\n{user_prompt}"


async def revise_candidate(
    package: HypothesisPackage,
    *,
    blocking_factor: str,
    debated_perspective: str,
    reviews: list[SkepticReport],
    prior_rounds: list[str],
    llm_sem: asyncio.Semaphore | None = None,
    model: Model | str | None = None,
) -> tuple[HypothesisPackage, RevisionDraft]:
    """跑一次修订，返回 (修订稿, RevisionDraft)。

    idea_id / sampling_probability / revision_round / lineage_op 全部由代码填，不向 LLM 索要：
    idea_id 必须保持不变（hard_gate 强制全套报告同 id），sampling_probability 原样搬运原候选的
    自评（reviser 无权给自己抬分）。

    修订内容构造不出合法 HypothesisPackage 时**向外抛出**（校验器要求 predictions 与
    disconfirmers 均非空），由调用方按 reviser 失败处理——绝不放宽校验器。

    Example:
        >>> revised, draft = await revise_candidate(package,
        ...     blocking_factor="risk_ok_methodology", debated_perspective="methodology",
        ...     reviews=reviews, prior_rounds=[])  # doctest: +SKIP
        >>> revised.lineage_op
        'revise'
    """
    prompt = build_revision_prompt(
        package, blocking_factor=blocking_factor, debated_perspective=debated_perspective,
        reviews=reviews, prior_rounds=prior_rounds,
    )
    async with limited_by(llm_sem):
        draft = await single_turn_chat(prompt, RevisionDraft, model=model)

    revised = HypothesisPackage(
        idea_id=package.idea_id,
        generation_strategy=package.generation_strategy,
        sampling_probability=package.sampling_probability,
        novel_hypothesis=draft.revised_novel_hypothesis,
        supported_premises=draft.revised_premises,
        inference_chain=package.inference_chain,
        predicted_observations=draft.revised_predicted_observations,
        disconfirming_observations=draft.revised_disconfirming_observations,
        validation_plan_ref=None,
        revision_round=package.revision_round + 1,
        lineage_op="revise",
    )
    return revised, draft


# ====== staleness：逐报告的输入指纹比对 ======

async def novelty_is_stale(
    package: HypothesisPackage,
    novelty: NoveltyEvidenceReport,
    *,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
) -> bool:
    """novelty 报告的输入是否已失效。用当前 package 重建检索提问、落盘取内容寻址 ref，与报告
    存档的 input_ref 比对——ArtifactStore 是 sha256 内容寻址，ref 相等即内容相等。

    input_ref 为 None（失败降级报告）一律判 stale：无法证明未失效就不能复用。

    Example:
        >>> await novelty_is_stale(package, novelty, artifacts=store,
        ...     corpus_ref=corpus_ref)  # doctest: +SKIP
        False
    """
    if novelty.input_ref is None:
        return True
    current = await artifacts.put_text(build_novelty_question(package, corpus_ref))
    return novelty.input_ref != current


async def stale_perspectives(
    package: HypothesisPackage,
    reviews: list[SkepticReport],
    *,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    prior_transcript: str,
) -> set[str]:
    """返回输入已失效、需要重跑的视角 id 集合。

    **prior_transcript 必须是刷新后当前有效的那份转录**：domain_consistency 的输入模板内嵌
    prior_retrieval，存档指纹编码的是旧转录，传入新转录才能让级联（novelty 重跑 →
    domain_consistency 也失效）自动成立。调用方因此必须按"先 novelty、再视角"的顺序调。

    Example:
        >>> await stale_perspectives(package, reviews, artifacts=store,
        ...     corpus_ref=corpus_ref, prior_transcript="")  # doctest: +SKIP
        set()
    """
    by_perspective = {r.perspective: r for r in reviews}
    stale: set[str] = set()
    for perspective in REVIEW_PERSPECTIVES:
        report = by_perspective.get(perspective.perspective_id)
        if report is None or report.input_ref is None:
            stale.add(perspective.perspective_id)
            continue
        current = await artifacts.put_text(build_perspective_input(
            package, perspective, corpus_ref=corpus_ref, prior_transcript=prior_transcript))
        if report.input_ref != current:
            stale.add(perspective.perspective_id)
    return stale


# ====== 辩论循环 ======

def build_rereview_prompt(
    package: HypothesisPackage,
    perspective: ReviewPerspective,
    previous: SkepticReport,
    draft: RevisionDraft,
    *,
    round_index: int,
    prior_transcript: str,
) -> str:
    """拼装对手视角的重表态 prompt。

    锚点是 ``Debate round <n> - perspective: <id>``，**绝不能写成
    ``Review perspective: <id>``**——后者在 prompts.py 里已有三处硬编码，测试的
    make_routed_model 要求命中数恰好为 1，撞上就直接抛 AssertionError。

    检索转录只对 needs_retrieval 的视角附上，且是**冻结的那份**：辩论轮不跑检索。

    Example:
        >>> build_rereview_prompt(package, perspective, previous, draft,
        ...     round_index=1, prior_transcript="")  # doctest: +SKIP
    """
    premise_lines = format_premise_lines(package)
    retrieval_context = (
        f"Retrieval transcript from your earlier review:\n{prior_transcript}"
        if perspective.needs_retrieval and prior_transcript else ""
    )
    user_prompt = DEBATE_REREVIEW_PROMPT_TEMPLATE.format(
        round_index=round_index,
        perspective_id=perspective.perspective_id,
        previous_critique=previous.critique,
        previous_risks="\n".join(f"- {r}" for r in previous.unaddressed_risks) or "(none)",
        rebuttal=draft.rebuttal,
        changes_made="\n".join(f"- {c}" for c in draft.changes_made) or "(none listed)",
        novel_hypothesis=package.novel_hypothesis,
        supported_premises=premise_lines,
        predicted_observations="\n".join(f"- {o}" for o in package.predicted_observations),
        disconfirming_observations="\n".join(
            f"- {o}" for o in package.disconfirming_observations),
        retrieval_context=retrieval_context,
    )
    return f"{DEBATE_REREVIEW_SYSTEM_PROMPT}\n\n{user_prompt}"


def blocked_item_cleared(blocking_factor: str, reviews: list[SkepticReport]) -> bool:
    """被拦项是否已清除。谓词全部从 gatekeeper 复用，本模块不重写任何阈值。公开（不加下划线）
    是因为它是一个独立的判定概念，且被 run_debate 每轮都调用一次——按代码规范，两次以上的调用
    不该藏在私有前缀后面，藏起来还会把 run_debate 的分支多嵌一层，顶到 4 层嵌套。

    Example:
        >>> blocked_item_cleared("risk_total", reviews)  # doctest: +SKIP
        True
    """
    if blocking_factor == RISK_TOTAL_ITEM:
        total = sum(len(r.unaddressed_risks) for r in reviews)
        return total <= MAX_TOTAL_RISKS
    perspective_id = blocking_factor[len(RISK_ITEM_PREFIX):]
    target = next((r for r in reviews if r.perspective == perspective_id), None)
    return target is not None and perspective_ok(target)


async def run_debate(
    package: HypothesisPackage,
    *,
    blocking_factor: str,
    reviews: list[SkepticReport],
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    prior_transcript: str,
    llm_sem: asyncio.Semaphore | None = None,
    model: Model | str | None = None,
) -> tuple[HypothesisPackage, list[SkepticReport], list[RevisionRound]]:
    """跑最多 MAX_DEBATE_ROUNDS 轮辩论，返回 (最终修订稿, 更新后的审阅列表, 逐轮记录)。

    每轮恒为 2 次 single_turn_chat、0 个检索循环。llm_sem 的名额在每次调用内部获取与释放，
    **绝不跨轮持有**——跨轮持有就是候选数 >= 名额数时必然死锁的那个模式。

    任何失败路径都只让候选停在原状：reviser 失败直接返回原 package/原 reviews/空 rounds，
    绝不存在"修订流程出错反而放行"的路径。

    Example:
        >>> final, reviews, rounds = await run_debate(package,
        ...     blocking_factor="risk_ok_methodology", reviews=reviews, artifacts=store,
        ...     corpus_ref=ref, prior_transcript="")  # doctest: +SKIP
    """
    opponent_id = select_debate_opponent(blocking_factor, reviews)
    perspective = next(p for p in REVIEW_PERSPECTIVES if p.perspective_id == opponent_id)

    current = package
    current_reviews = list(reviews)
    rounds: list[RevisionRound] = []
    prior_summaries: list[str] = []

    for round_index in range(1, MAX_DEBATE_ROUNDS + 1):
        previous = next(r for r in current_reviews if r.perspective == opponent_id)
        try:
            revised, draft = await revise_candidate(
                current, blocking_factor=blocking_factor, debated_perspective=opponent_id,
                reviews=current_reviews, prior_rounds=prior_summaries,
                llm_sem=llm_sem, model=model,
            )
        except Exception:  # noqa: BLE001 - provider 报错与修订稿校验失败形态不定，一律停止辩论
            break

        rebuttal_ref = await artifacts.put_text(draft.rebuttal)
        if is_no_op_revision(draft, current):
            rounds.append(RevisionRound(
                round_index=round_index, debated_perspective=opponent_id,
                package_ref=await artifacts.put_text(current.model_dump_json()),
                rebuttal_ref=rebuttal_ref, reviewer_response_ref=None, cleared=False,
            ))
            break

        prompt = build_rereview_prompt(
            revised, perspective, previous, draft,
            round_index=round_index, prior_transcript=prior_transcript,
        )
        async with limited_by(llm_sem):
            judgment = await single_turn_chat(prompt, SkepticJudgment, model=model)

        # 设计 §4.4：存**规范化**输入的指纹，不是辩论 prompt 的指纹——这让被辩视角在终局
        # 天然复用，同时 novelty 重跑换了转录时 domain_consistency 仍会正确失效
        canonical_ref = await artifacts.put_text(build_perspective_input(
            revised, perspective, corpus_ref=corpus_ref, prior_transcript=prior_transcript))
        response = SkepticReport(
            idea_id=revised.idea_id, perspective=opponent_id, critique=judgment.critique,
            unaddressed_risks=judgment.unaddressed_risks,
            fatal_flaw_found=judgment.fatal_flaw_found,
            transcript_ref=previous.transcript_ref, input_ref=canonical_ref,
        )
        current = revised
        current_reviews = [response if r.perspective == opponent_id else r
                           for r in current_reviews]
        cleared = blocked_item_cleared(blocking_factor, current_reviews)
        rounds.append(RevisionRound(
            round_index=round_index, debated_perspective=opponent_id,
            package_ref=await artifacts.put_text(revised.model_dump_json()),
            rebuttal_ref=rebuttal_ref,
            reviewer_response_ref=await artifacts.put_text(judgment.model_dump_json()),
            cleared=cleared,
        ))
        prior_summaries.append(
            f"round {round_index}: rebuttal={draft.rebuttal} | "
            f"reviewer replied={judgment.critique} | remaining risks={judgment.unaddressed_risks}"
        )
        if cleared:
            break

    return current, current_reviews, rounds


# ====== 终局刷新：只重跑输入确已失效的报告 ======

async def refresh_stale_evidence(
    package: HypothesisPackage,
    *,
    problem_domain: str,
    novelty: NoveltyEvidenceReport,
    reviews: list[SkepticReport],
    novelty_agent: Agent,
    domain_review_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    llm_sem: asyncio.Semaphore | None = None,
    retrieval_sem: asyncio.Semaphore | None = None,
    model: Model | str | None = None,
) -> tuple[HypothesisPackage, StructuralCheckReport, FalsifiabilityReport,
           NoveltyEvidenceReport, list[SkepticReport], ValidationPlan]:
    """辩论结束后重跑输入已失效的报告，返回终审所需的整套证据（不产出 verdict）。

    **顺序必须是"先 novelty、再视角"**：novelty 重跑会换掉检索转录，而 domain_consistency 的
    输入内嵌该转录；用刷新后的转录去判视角失效，级联才成立——反过来做，或者拿旧转录判视角，
    级联会悄无声息地不成立，一份建立在过时检索证据上的报告就会被当作最新的直接放行。

    structural / falsifiability / validation_plan 无条件重跑——前两者一个是纯函数、一个是
    1 次 single_turn_chat，validation_plan 是纯规则 + artifact 落盘，判定成本与执行成本同
    量级，加判定只是复杂化。

    Example:
        >>> pkg, structural, fals, novelty, reviews, plan = await refresh_stale_evidence(
        ...     package, problem_domain="ai4s", novelty=novelty, reviews=reviews,
        ...     novelty_agent=a1, domain_review_agent=a2, artifacts=store,
        ...     corpus_ref=ref)  # doctest: +SKIP
    """
    if await novelty_is_stale(package, novelty, artifacts=artifacts, corpus_ref=corpus_ref):
        try:
            novelty = await collect_novelty_evidence(
                package, agent=novelty_agent, artifacts=artifacts, corpus_ref=corpus_ref,
                llm_sem=llm_sem, retrieval_sem=retrieval_sem, model=model,
            )
        except Exception as error:  # noqa: BLE001 - 与 _audit_candidate 同一条降级
            novelty = await degraded_novelty_report(package.idea_id, error, artifacts)

    prior_transcript = await read_prior_transcript(novelty, artifacts)
    stale = await stale_perspectives(
        package, reviews, artifacts=artifacts, corpus_ref=corpus_ref,
        prior_transcript=prior_transcript,
    )
    # 只建一次有序的 stale 视角列表，gather 的入参与之后的结果配对全部复用这同一份列表——
    # 两处各写一遍再指望顺序一致是自找的隐患，任何一处漏改都会把结果错配到别的视角上
    stale_ordered = [p for p in REVIEW_PERSPECTIVES if p.perspective_id in stale]
    by_perspective = {r.perspective: r for r in reviews}
    refreshed = await asyncio.gather(*[
        review_one_perspective(
            package, perspective, novelty=novelty,
            domain_review_agent=domain_review_agent, artifacts=artifacts,
            corpus_ref=corpus_ref, llm_sem=llm_sem, retrieval_sem=retrieval_sem, model=model,
        )
        for perspective in stale_ordered
    ], return_exceptions=True)
    for perspective, outcome in zip(stale_ordered, refreshed):
        by_perspective[perspective.perspective_id] = outcome if isinstance(
            outcome, SkepticReport
        ) else SkepticReport(
            idea_id=package.idea_id, perspective=perspective.perspective_id,
            critique=f"refresh failed: {outcome}", unaddressed_risks=[],
            fatal_flaw_found=False, failed=True,
        )
    ordered_reviews = [by_perspective[p.perspective_id] for p in REVIEW_PERSPECTIVES]

    structural = structural_check(package)
    try:
        async with limited_by(llm_sem):
            falsifiability = await falsifiability_check(package, model=model)
    except Exception as error:  # noqa: BLE001 - 与 _screen_candidate 同一条降级
        falsifiability = degraded_falsifiability_report(package.idea_id, error)
    verifier = match_verifier(package, problem_domain)
    validation_plan = await plan_validation(package, verifier, artifacts=artifacts)
    plan_ref = await artifacts.put_text(validation_plan.model_dump_json())
    package = package.model_copy(update={"validation_plan_ref": plan_ref})
    return package, structural, falsifiability, novelty, ordered_reviews, validation_plan

"""Quality gates inserted between the production Ideator's output and
``Supervisor.register_hypotheses``.

Two gates live here:

- ``gate_hypotheses``: the original minimal gate — a bare falsifiability check directly
  against ``core.research_models.Hypothesis`` fields. Kept for callers that only have the
  plain ``HypothesisBatch`` shape (no premises/predictions/disconfirmers).
- ``run_light_pipeline``: the real integration point, run against the richer
  ``IdeatorHypothesisBatch`` (see ``idea_schemas.IdeatorHypothesisDraft``) the live Ideator
  now produces (``core/agent/prompts/ideator_agent.md`` + ``agents/ideator_agent.py`` were
  updated together with this module). Runs pre_gate (structural + falsifiability) ->
  methodology+statistics review (skips domain_consistency, which needs a paper_rag corpus
  that isn't wired into the live composition root) -> validation planning ->
  ``gatekeeper.light_hard_gate`` -> pairwise rank the survivors.

Corpus-dependent steps intentionally not run: gap_mining, novelty audit, domain_consistency
review, and the REVISE debate loop (which needs all three review perspectives). A candidate
that ``light_hard_gate`` sends to REVISE is simply dropped for this round, not retried —
scoped this way to ship a real, working gate now rather than adapt the full revise loop to a
two-perspective variant it wasn't designed for.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.research_models import Hypothesis
from athena.research.idea_generation.evidence_retrieval import (
    collect_novelty_evidence,
    degraded_novelty_report,
)
from athena.research.idea_generation.gatekeeper import hard_gate, light_hard_gate, pre_gate
from athena.research.idea_generation.idea_schemas import (
    FalsifiabilityJudgment,
    GateVerdict,
    HypothesisPackage,
    IdeatorHypothesisDraft,
    PairwiseJudgment,
)
from athena.research.idea_generation.pre_gate_checks import (
    degraded_falsifiability_report,
    falsifiability_check,
    structural_check,
)
from athena.research.idea_generation.prompts import (
    PAIRWISE_JUDGE_SYSTEM_PROMPT,
    PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE,
)
from athena.research.idea_generation.ranking import HypoPriList, PairwiseComparison
from athena.research.idea_generation.review_board import REVIEW_PERSPECTIVES, review_or_degrade
from athena.research.idea_generation.structured_chat import single_turn_structured_chat
from athena.research.idea_generation.validation import match_verifier, plan_validation

_GATE_PROMPT_TEMPLATE = (
    "You are a skeptical falsifiability auditor. A hypothesis generator proposed the "
    "following research hypothesis. Decide whether a genuinely testable implication "
    "exists and list any variables that would be unobservable in practice. Do not "
    "assume good faith; look for hidden unobservable variables.\n\n"
    "Statement: {statement}\n"
    "Intervention: {intervention}\n"
    "Expected effect: {expected_effect}\n\n"
    "Assess whether this hypothesis is falsifiable in practice."
)


async def gate_hypotheses(
    hypotheses: list[Hypothesis], *, model: str, artifacts: ArtifactStore,
) -> list[Hypothesis]:
    """Filter ``hypotheses`` down to those that pass a falsifiability check.

    A hypothesis whose check call itself fails (provider error, invalid structured
    output) is dropped rather than raised — one bad candidate must not fail the whole
    Ideator turn, matching ``pre_gate_checks.degraded_falsifiability_report``'s
    fail-closed contract for the rest of this pipeline.

    Example:
        >>> kept = await gate_hypotheses([h1, h2], model="m", artifacts=store)  # doctest: +SKIP
        >>> all(h in [h1, h2] for h in kept)
        True
    """
    kept: list[Hypothesis] = []
    for hypothesis in hypotheses:
        prompt = _GATE_PROMPT_TEMPLATE.format(
            statement=hypothesis.statement, intervention=hypothesis.intervention,
            expected_effect=hypothesis.expected_effect,
        )
        try:
            judgment = await single_turn_structured_chat(
                prompt, FalsifiabilityJudgment, model=model, artifacts=artifacts,
            )
        except Exception:  # noqa: BLE001 - provider 报错形态不定，一律降级为不通过
            continue
        if judgment.is_falsifiable:
            kept.append(hypothesis)
    return kept


# ====== run_light_pipeline: pre_gate + methodology/statistics review + light_hard_gate ======

LLM_CONCURRENCY: int = 8
"""每个 Ideator lane 内部的并发上限；lane 本身已经在 count 层面很小（<=5 条草稿），
不需要 workflow.py 全流程那种 16 的量级。"""

RETRIEVAL_CONCURRENCY: int = 4
"""带检索工具的 Agent 循环并发上限。名额只在单个检索循环粒度上获取与释放，绝不跨越
novelty -> domain_consistency 的依赖边界持有——持有一个的同时等第二个，在候选数 >= 名额数
时必然死锁（见 evidence_retrieval.limited_by 的注释）。"""

ProgressFn = Callable[[str], Awaitable[None]]
"""进度回调：门禁全程只有 LLM 往返、没有本地计算，不报进度的话"正在跑十几个调用"与
"卡死了"在外部看来完全一样（本次会话真的据此误判过一次）。只报小摘要，不带 payload——
与"大对象只用 ArtifactRef 引用"是同一条规则在事件流上的落点。"""


async def _silent(_message: str) -> None:
    """默认不报进度：单测与库内调用不需要观测面。"""


def _blocking_evidence(decision) -> str:
    """取出拦住候选的那一项 rubric 的证据文本，作为反馈给生成侧的具体理由。

    只有逐项证据才能让生成侧知道该改什么；只回一个 blocking_factor 名字等于让它猜。
    """
    for item in decision.item_scores:
        if item.item == decision.blocking_factor:
            return item.evidence
    return "no itemized evidence recorded"


LIGHT_VERIFIER_DOMAIN: str = "machine_learning"
"""没有真实任务 domain 概念（core.Hypothesis 不带这个字段）时的固定取值,匹配
validation.BUILTIN_VERIFIERS 里的 ablation_replication——这正是"改一个特征/模型，比较
改前改后指标"的场景，与 SEARCH 循环的语义天然吻合。"""


def _build_package(draft: IdeatorHypothesisDraft) -> HypothesisPackage:
    """Assign idea_id/generation_strategy/lineage_op by code, matching the convention that
    the LLM never authors identifiers or bookkeeping fields (see idea_schemas.py module
    docstring)."""
    return HypothesisPackage(
        idea_id=f"idea-{uuid.uuid4().hex[:12]}",
        generation_strategy="eda_grounded",
        novel_hypothesis=draft.statement,
        supported_premises=draft.supported_premises,
        inference_chain=draft.inference_chain,
        predicted_observations=draft.predicted_observations,
        disconfirming_observations=draft.disconfirming_observations,
        lineage_op="generate",
        sources=draft.sources,
    )


def _to_core_hypothesis(draft: IdeatorHypothesisDraft, package: HypothesisPackage) -> Hypothesis:
    """HypothesisPackage carries the audit trail (premises/predictions/disconfirmers) but
    not intervention/expected_effect — those live on the original draft, so the final
    core.Hypothesis is built from both."""
    evidence_refs = [
        ref for premise in package.supported_premises for ref in premise.supporting_refs
    ]
    return Hypothesis(
        statement=draft.statement, intervention=draft.intervention,
        expected_effect=draft.expected_effect, evidence_refs=evidence_refs,
        sources=package.sources,
    )


async def _screen_and_review(
    draft: IdeatorHypothesisDraft, *, model: str, artifacts: ArtifactStore,
    llm_sem: asyncio.Semaphore, retrieval_sem: asyncio.Semaphore,
    corpus_ref: ArtifactRef | None, novelty_agent: object | None,
    domain_review_agent: object | None, progress: ProgressFn,
    rejections: list[str],
) -> tuple[IdeatorHypothesisDraft, HypothesisPackage] | None:
    """Run one draft through pre_gate -> review -> gate.

    Two shapes, picked by whether a literature corpus is available:

    - no corpus: methodology + statistics review, then ``light_hard_gate`` (no novelty_ok).
    - corpus: novelty audit first, then all three review perspectives (domain_consistency
      reuses the novelty retrieval transcript), then the full ``hard_gate``.

    Returns None for REVISE/REJECT — dropped for this round, not retried (see module
    docstring for why the revise loop is out of scope here).
    """
    package = _build_package(draft)
    await progress(f"falsifiability check: {package.idea_id}")
    async with llm_sem:
        structural = structural_check(package)
        try:
            falsifiability = await falsifiability_check(package, model=model, artifacts=artifacts)
        except Exception as error:  # noqa: BLE001 - 与 pre_gate_checks 其余调用点同一条降级
            falsifiability = degraded_falsifiability_report(package.idea_id, error)

    decision = pre_gate(structural, falsifiability)
    if decision.verdict != GateVerdict.PASS:
        await progress(
            f"pre_gate dropped {package.idea_id}: {decision.blocking_factor}"
        )
        rejections.append(
            f"{draft.statement!r} was rejected at pre_gate on "
            f"{decision.blocking_factor}: {_blocking_evidence(decision)}"
        )
        return None

    # 语料缺席时 novelty 保持 None：review_or_degrade 只对 needs_retrieval 的视角用它，
    # 而那个视角此时根本不参与。绝不构造一份空 novelty 报告冒充"查过了"。
    novelty = None
    if corpus_ref is not None:
        await progress(f"novelty audit: {package.idea_id}")
        try:
            novelty = await collect_novelty_evidence(
                package, agent=novelty_agent, artifacts=artifacts, corpus_ref=corpus_ref,
                llm_sem=llm_sem, retrieval_sem=retrieval_sem, model=model,
            )
        except Exception as error:  # noqa: BLE001 - 检索失败降级为空报告，不拖垮这个候选
            novelty = await degraded_novelty_report(package.idea_id, error, artifacts)

    perspectives = REVIEW_PERSPECTIVES if corpus_ref is not None else REVIEW_PERSPECTIVES[:2]
    await progress(
        f"review ({len(perspectives)} perspectives): {package.idea_id}"
    )
    reviews = await asyncio.gather(*[
        review_or_degrade(
            package, perspective, novelty=novelty, domain_review_agent=domain_review_agent,
            artifacts=artifacts, corpus_ref=corpus_ref or "", llm_sem=llm_sem,
            retrieval_sem=retrieval_sem, model=model,
        )
        for perspective in perspectives
    ])

    verifier = match_verifier(package, LIGHT_VERIFIER_DOMAIN)
    validation_plan = await plan_validation(package, verifier, artifacts=artifacts)

    if novelty is not None:
        decision = hard_gate(
            structural, falsifiability, novelty, list(reviews), validation_plan
        )
    else:
        decision = light_hard_gate(structural, falsifiability, list(reviews), validation_plan)
    if decision.verdict not in (GateVerdict.PASS, GateVerdict.EXPLORATORY):
        await progress(
            f"gate {decision.verdict.value} {package.idea_id}: {decision.blocking_factor}"
        )
        rejections.append(
            f"{draft.statement!r} was {decision.verdict.value} on "
            f"{decision.blocking_factor}: {_blocking_evidence(decision)}"
        )
        return None
    await progress(f"gate {decision.verdict.value}: {package.idea_id}")
    return draft, package


async def _pairwise_compare(
    statement_a: str, statement_b: str, key_a: str, key_b: str, *,
    llm_sem: asyncio.Semaphore, artifacts: ArtifactStore, model: str,
) -> PairwiseComparison:
    """Anonymized, bidirectional pairwise judgment — same bias-avoidance pattern as
    workflow._pairwise_compare, keyed on local ranking ids rather than idea_id."""
    forward_prompt = "\n\n".join([
        PAIRWISE_JUDGE_SYSTEM_PROMPT,
        PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE.format(candidate_a=statement_a, candidate_b=statement_b),
    ])
    backward_prompt = "\n\n".join([
        PAIRWISE_JUDGE_SYSTEM_PROMPT,
        PAIRWISE_JUDGE_USER_PROMPT_TEMPLATE.format(candidate_a=statement_b, candidate_b=statement_a),
    ])

    async def _run(prompt: str) -> PairwiseJudgment:
        async with llm_sem:
            return await single_turn_structured_chat(
                prompt, PairwiseJudgment, model=model, artifacts=artifacts,
            )

    forward, backward = await asyncio.gather(_run(forward_prompt), _run(backward_prompt))
    forward_winner = key_a if forward.winner == "candidate_a" else key_b
    backward_winner = key_b if backward.winner == "candidate_a" else key_a
    winner_id = forward_winner
    rationale = (
        f"forward+backward agree: {forward.rationale} | {backward.rationale}"
        if forward_winner == backward_winner
        else f"disagreement (possible position bias); forward={forward_winner}, backward={backward_winner}"
    )
    return PairwiseComparison(idea_id_a=key_a, idea_id_b=key_b, winner_id=winner_id, rationale=rationale)


async def run_light_pipeline(
    drafts: list[IdeatorHypothesisDraft], *, model: str, artifacts: ArtifactStore,
    corpus_ref: ArtifactRef | None = None, novelty_agent: object | None = None,
    domain_review_agent: object | None = None, progress: ProgressFn = _silent,
    rejections: list[str] | None = None,
) -> list[Hypothesis]:
    """Screen/review/gate every draft, then rank survivors by pairwise Elo (best first).

    ``corpus_ref`` 缺席（默认）时行为与接入语料前逐字节一致：pre_gate + methodology/
    statistics 两个视角 + ``light_hard_gate``。给出 ``corpus_ref`` 时额外跑新颖性审计与
    domain_consistency 审阅，并改用完整 ``hard_gate``（多一项 novelty_ok）——这是语料带
    来的唯一新增判据。

    半配置（给了 corpus_ref 却没给 Agent）直接抛错，不静默降级：那会悄无声息地丢掉
    新颖性门槛，让调用方以为自己开了而其实没开。

    Never raises for per-candidate failures: drafts whose checks fail outright are dropped
    (fail-closed, matching the rest of this pipeline).

    Example:
        >>> kept = await run_light_pipeline([d1, d2], model="m", artifacts=store)  # doctest: +SKIP
        >>> all(isinstance(h, Hypothesis) for h in kept)
        True
    """
    if corpus_ref is not None and (novelty_agent is None or domain_review_agent is None):
        raise ValueError(
            "corpus_ref requires both novelty_agent and domain_review_agent; refusing to "
            "silently drop the novelty gate"
        )

    llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)
    retrieval_sem = asyncio.Semaphore(RETRIEVAL_CONCURRENCY)
    # 调用方传入 list 即可收集逐条拒绝理由，用来反馈给生成侧重新提案。
    collected = rejections if rejections is not None else []
    outcomes = await asyncio.gather(*[
        _screen_and_review(
            draft, model=model, artifacts=artifacts, llm_sem=llm_sem,
            retrieval_sem=retrieval_sem, corpus_ref=corpus_ref,
            novelty_agent=novelty_agent, domain_review_agent=domain_review_agent,
            progress=progress, rejections=collected,
        )
        for draft in drafts
    ], return_exceptions=True)
    survivors = [o for o in outcomes if isinstance(o, tuple)]
    await progress(f"gate kept {len(survivors)}/{len(drafts)} candidates")
    if len(survivors) <= 1:
        return [_to_core_hypothesis(draft, package) for draft, package in survivors]

    keys = [f"survivor-{i}" for i in range(len(survivors))]
    book = HypoPriList()
    for key in keys:
        book.ensure_registered(key)
    pairs = [(i, j) for i in range(len(survivors)) for j in range(i + 1, len(survivors))]
    comparisons = await asyncio.gather(*[
        _pairwise_compare(
            survivors[i][1].novel_hypothesis, survivors[j][1].novel_hypothesis,
            keys[i], keys[j], llm_sem=llm_sem, artifacts=artifacts, model=model,
        )
        for i, j in pairs
    ], return_exceptions=True)
    for comparison in comparisons:
        if isinstance(comparison, PairwiseComparison):
            book.record_comparison(comparison)

    by_key = dict(zip(keys, survivors))
    ranked_keys = [rc.idea_id for rc in book.rank()]
    return [_to_core_hypothesis(*by_key[key]) for key in ranked_keys]

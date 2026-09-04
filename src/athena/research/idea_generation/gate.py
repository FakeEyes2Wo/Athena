"""Quality gate inserted between the production Ideator's output and
``Supervisor.register_hypotheses``.

``run_light_pipeline`` is the integration point, run against the richer
``IdeatorHypothesisBatch`` the live Ideator produces. It runs pre_gate (structural +
falsifiability) -> methodology+statistics review -> validation planning ->
``gatekeeper.light_hard_gate``, then returns the survivors in the same order they were
submitted. There is deliberately no pipeline-local ranking: the shared
``ResearchTree``/Supervisor hypothesis pool is the single ordering surface, exactly like
the debate Ideator.

A candidate that ``light_hard_gate`` sends to REVISE/REJECT is dropped for this round,
not retried inside the pipeline — the retry loop lives one level up in
``turns.ideator``, which re-prompts the same Ideator with the recorded blocking reasons.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from athena.core.contracts import ArtifactStore
from athena.core.research_models import Hypothesis
from athena.research.idea_generation.gatekeeper import light_hard_gate, pre_gate
from athena.research.idea_generation.idea_schemas import (
    GateVerdict,
    HypothesisPackage,
    IdeatorHypothesisDraft,
)
from athena.research.idea_generation.pre_gate_checks import (
    degraded_falsifiability_report,
    falsifiability_check,
    structural_check,
)
from athena.research.idea_generation.review_board import (
    REVIEW_PERSPECTIVES,
    review_or_degrade,
)
from athena.research.idea_generation.validation import match_verifier, plan_validation

LLM_CONCURRENCY: int = 8
"""每个 Ideator lane 内部的并发上限；lane 本身已经在 count 层面很小（<=5 条草稿），
不需要全流程级的大并发。"""

ProgressFn = Callable[[str], Awaitable[None]]
"""进度回调：门禁全程只有 LLM 往返、没有本地计算，不报进度的话"正在跑十几个调用"与
"卡死了"在外部看来完全一样。只报小摘要，不带 payload——与"大对象只用 ArtifactRef 引用"
是同一条规则在事件流上的落点。"""


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


def _to_core_hypothesis(
    draft: IdeatorHypothesisDraft, package: HypothesisPackage
) -> Hypothesis:
    """HypothesisPackage carries the audit trail (premises/predictions/disconfirmers) but
    not intervention/expected_effect — those live on the original draft, so the final
    core.Hypothesis is built from both."""
    evidence_refs = [
        ref for premise in package.supported_premises for ref in premise.supporting_refs
    ]
    return Hypothesis(
        statement=draft.statement,
        intervention=draft.intervention,
        expected_effect=draft.expected_effect,
        evidence_refs=evidence_refs,
        sources=package.sources,
    )


async def _screen_and_review(
    draft: IdeatorHypothesisDraft,
    *,
    model: str,
    artifacts: ArtifactStore,
    client: Any,
    llm_sem: asyncio.Semaphore,
    progress: ProgressFn,
    rejections: list[str],
) -> tuple[IdeatorHypothesisDraft, HypothesisPackage] | None:
    """Run one draft through pre_gate -> methodology/statistics review -> light_hard_gate.

    Returns None for REVISE/REJECT — dropped for this round; the retry loop is owned by
    ``turns.ideator``, not by this pipeline.
    """
    package = _build_package(draft)
    await progress(f"falsifiability check: {package.idea_id}")
    async with llm_sem:
        structural = structural_check(package)
        try:
            falsifiability = await falsifiability_check(
                package, model=model, artifacts=artifacts, client=client
            )
        except (
            Exception
        ) as error:  # noqa: BLE001 - 与 pre_gate_checks 其余调用点同一条降级
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

    await progress(
        f"review ({len(REVIEW_PERSPECTIVES)} perspectives): {package.idea_id}"
    )
    reviews = await asyncio.gather(
        *[
            review_or_degrade(
                package,
                perspective,
                artifacts=artifacts,
                llm_sem=llm_sem,
                model=model,
                client=client,
            )
            for perspective in REVIEW_PERSPECTIVES
        ]
    )

    verifier = match_verifier(package, LIGHT_VERIFIER_DOMAIN)
    validation_plan = await plan_validation(package, verifier, artifacts=artifacts)

    decision = light_hard_gate(
        structural, falsifiability, list(reviews), validation_plan
    )
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


async def run_light_pipeline(
    drafts: list[IdeatorHypothesisDraft],
    *,
    model: str,
    artifacts: ArtifactStore,
    client: Any = None,
    progress: ProgressFn = _silent,
    rejections: list[str] | None = None,
) -> list[Hypothesis]:
    """Screen/review/gate every draft and return the survivors in submission order.

    There is no pipeline-local ranking: the shared ResearchTree/Supervisor hypothesis pool
    owns ordering, the same surface the debate Ideator uses. Never raises for per-candidate
    failures: drafts whose checks fail outright are dropped (fail-closed, matching the rest
    of this pipeline). ``rejections``, when provided, collects the itemized reason each
    dropped candidate was blocked, so the caller can feed them back to the generator.

    Example:
        >>> kept = await run_light_pipeline([d1, d2], model="m", artifacts=store)  # doctest: +SKIP
        >>> all(isinstance(h, Hypothesis) for h in kept)
        True
    """
    llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)
    # 调用方传入 list 即可收集逐条拒绝理由，用来反馈给生成侧重新提案。
    collected = rejections if rejections is not None else []
    outcomes = await asyncio.gather(
        *[
            _screen_and_review(
                draft,
                model=model,
                artifacts=artifacts,
                client=client,
                llm_sem=llm_sem,
                progress=progress,
                rejections=collected,
            )
            for draft in drafts
        ],
        return_exceptions=True,
    )
    survivors = [o for o in outcomes if isinstance(o, tuple)]
    await progress(f"gate kept {len(survivors)}/{len(drafts)} candidates")
    return [_to_core_hypothesis(draft, package) for draft, package in survivors]

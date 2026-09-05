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
from dataclasses import dataclass

from athena.core.agent.chat import single_turn_structured_chat
from athena.core.contracts import ArtifactStore
from athena.core.research_models import Hypothesis
from athena.research.idea_generation.gatekeeper import light_hard_gate, pre_gate
from athena.research.idea_generation.idea_schemas import (
    ClaimRole,
    FalsifiabilityJudgment,
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    IdeatorHypothesisDraft,
)
from athena.research.idea_generation.review_board import (
    REVIEW_PERSPECTIVES,
    review_or_degrade,
)

LLM_CONCURRENCY: int = 8
"""每个 Ideator lane 内部的并发上限；lane 本身已经在 count 层面很小（<=5 条草稿），
不需要全流程级的大并发。"""

ProgressFn = Callable[[str], Awaitable[None]]
"""进度回调：门禁全程只有 LLM 往返、没有本地计算，不报进度的话"正在跑十几个调用"与
"卡死了"在外部看来完全一样。只报小摘要，不带 payload——与"大对象只用 ArtifactRef 引用"
是同一条规则在事件流上的落点。"""


async def _silent(_message: str) -> None:
    """默认不报进度：单测与库内调用不需要观测面。"""


@dataclass(slots=True)
class _GateRun:
    model: str
    artifacts: ArtifactStore
    llm_sem: asyncio.Semaphore
    progress: ProgressFn
    rejections: list[str]


_FALSIFIABILITY_PROMPT = (
    "You are a skeptical falsifiability auditor. Given a hypothesis's predicted and "
    "disconfirming observations, decide whether a genuinely testable implication "
    "exists and list any variables that would be unobservable in practice. Do not "
    "assume good faith; look for hidden unobservable variables.\n\n"
    "Novel hypothesis: {statement}\n"
    "Predicted observations:\n{predictions}\n"
    "Disconfirming observations:\n{disconfirmers}\n\n"
    "Assess whether this hypothesis is falsifiable in practice."
)


def _blocking_evidence(decision: GateDecision) -> str:
    """取出拦住候选的那一项 rubric 的证据文本，作为反馈给生成侧的具体理由。

    只有逐项证据才能让生成侧知道该改什么；只回一个 blocking_factor 名字等于让它猜。
    """
    for item in decision.item_scores:
        if item.item == decision.blocking_factor:
            return item.evidence
    return "no itemized evidence recorded"


def _to_core_hypothesis(draft: IdeatorHypothesisDraft) -> Hypothesis:
    """Convert a screened Ideator draft into the shared core hypothesis."""
    evidence_refs = [
        ref for premise in draft.supported_premises for ref in premise.supporting_refs
    ]
    return Hypothesis(
        statement=draft.statement,
        intervention=draft.intervention,
        expected_effect=draft.expected_effect,
        evidence_refs=evidence_refs,
        sources=draft.sources,
    )


def _premises_supported(draft: IdeatorHypothesisDraft) -> bool:
    """Return whether the draft contains an evidence-bound supported premise."""
    return any(
        claim.role == ClaimRole.SUPPORTED_PREMISE and claim.supporting_refs
        for claim in draft.supported_premises
    )


async def _check_falsifiability(
    draft: IdeatorHypothesisDraft, idea_id: str, run: _GateRun
) -> FalsifiabilityReport:
    prompt = _FALSIFIABILITY_PROMPT.format(
        statement=draft.statement,
        predictions="\n".join(f"- {item}" for item in draft.predicted_observations),
        disconfirmers="\n".join(
            f"- {item}" for item in draft.disconfirming_observations
        ),
    )
    judgment = await single_turn_structured_chat(
        prompt,
        FalsifiabilityJudgment,
        model=run.model,
        artifacts=run.artifacts,
    )
    return FalsifiabilityReport(idea_id=idea_id, **judgment.model_dump())


async def _screen_and_review(
    draft: IdeatorHypothesisDraft, run: _GateRun
) -> IdeatorHypothesisDraft | None:
    """Run one draft through pre_gate -> methodology/statistics review -> light_hard_gate.

    Returns None for REVISE/REJECT — dropped for this round; the retry loop is owned by
    ``turns.ideator``, not by this pipeline.
    """
    idea_id = f"idea-{uuid.uuid4().hex[:12]}"
    await run.progress(f"falsifiability check: {idea_id}")
    async with run.llm_sem:
        premise_evidence_ok = _premises_supported(draft)
        try:
            falsifiability = await _check_falsifiability(draft, idea_id, run)
        except Exception as error:  # noqa: BLE001 - provider failures fail closed
            falsifiability = FalsifiabilityReport(
                idea_id=idea_id,
                testable_implication="",
                unobservable_variables=[f"falsifiability check failed: {error}"],
                is_falsifiable=False,
            )

    decision = pre_gate(premise_evidence_ok, falsifiability)
    if decision.verdict != GateVerdict.PASS:
        await run.progress(f"pre_gate dropped {idea_id}: {decision.blocking_factor}")
        run.rejections.append(
            f"{draft.statement!r} was rejected at pre_gate on "
            f"{decision.blocking_factor}: {_blocking_evidence(decision)}"
        )
        return None

    await run.progress(f"review ({len(REVIEW_PERSPECTIVES)} perspectives): {idea_id}")
    reviews = await asyncio.gather(
        *[
            review_or_degrade(
                draft,
                idea_id,
                perspective,
                artifacts=run.artifacts,
                llm_sem=run.llm_sem,
                model=run.model,
            )
            for perspective in REVIEW_PERSPECTIVES
        ]
    )

    decision = light_hard_gate(premise_evidence_ok, falsifiability, list(reviews))
    if decision.verdict != GateVerdict.PASS:
        await run.progress(
            f"gate {decision.verdict.value} {idea_id}: {decision.blocking_factor}"
        )
        run.rejections.append(
            f"{draft.statement!r} was {decision.verdict.value} on "
            f"{decision.blocking_factor}: {_blocking_evidence(decision)}"
        )
        return None
    await run.progress(f"gate {decision.verdict.value}: {idea_id}")
    return draft


async def run_light_pipeline(
    drafts: list[IdeatorHypothesisDraft],
    *,
    model: str,
    artifacts: ArtifactStore,
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
    run = _GateRun(
        model=model,
        artifacts=artifacts,
        llm_sem=asyncio.Semaphore(LLM_CONCURRENCY),
        progress=progress,
        rejections=rejections if rejections is not None else [],
    )
    outcomes = await asyncio.gather(
        *[_screen_and_review(draft, run) for draft in drafts],
        return_exceptions=True,
    )
    survivors = [item for item in outcomes if isinstance(item, IdeatorHypothesisDraft)]
    await progress(f"gate kept {len(survivors)}/{len(drafts)} candidates")
    return [_to_core_hypothesis(draft) for draft in survivors]

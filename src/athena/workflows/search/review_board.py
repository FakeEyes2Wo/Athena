"""多视角并行审阅（ReviewBoard，步骤 [6]）：把 P2 的单一反方审阅函数换成三个各自独立
上下文、独立工具权限的视角，每个视角在 hard_gate 里各占一项 rubric。

呼应 Co-Scientist"生成与审阅分离、审阅侧不看生成侧自评概率"的设计：三个视角的 prompt 都不
携带 sampling_probability。工具按需分配——只有 domain_consistency 需要包外事实，另外两个靠
包内信息就能判定，用单轮调用即可，遵循 docs/design.md 的 Occam's razor。
"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.models import Model

from athena.core.agent import Agent, create_agent
from athena.core.schemas import ArtifactRef
from athena.core.tool import ToolRegistry
from athena.storage.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    DOMAIN_CONSISTENCY_QUESTION_TEMPLATE,
    DOMAIN_CONSISTENCY_SUMMARY_PROMPT_TEMPLATE,
    REVIEW_DOMAIN_CONSISTENCY_SYSTEM_PROMPT,
    REVIEW_METHODOLOGY_SYSTEM_PROMPT,
    REVIEW_PERSPECTIVE_HEADER_TEMPLATE,
    REVIEW_STATISTICS_SYSTEM_PROMPT,
    SKEPTIC_REVIEW_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.evidence_retrieval import limited_by, run_retrieval_agent
from athena.workflows.search.idea_schemas import (
    HypothesisPackage,
    NoveltyEvidenceReport,
    SkepticJudgment,
    SkepticReport,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


# ====== 视角定义 ======

@dataclass(frozen=True)
class ReviewPerspective:
    """一个审阅视角：id 同时用作 rubric 项后缀 risk_ok_<perspective_id>。

    Example:
        >>> ReviewPerspective("methodology", "system prompt", needs_retrieval=False).needs_retrieval
        False
    """
    perspective_id: str
    system_prompt: str
    needs_retrieval: bool


MAX_REVIEW_ATTEMPTS: int = 2
"""单个视角审阅的最大尝试次数。视角从 1 个变 3 个后，fail-closed 之下单次 provider 抖动把
候选打成 REVISE 的概率大致翻三倍。沿用生成路径 MAX_GENERATION_ATTEMPTS = 2 的先例；
fail-closed 规则本身不变——重试用尽仍失败就照常标 failed=True。"""


REVIEW_PERSPECTIVES: tuple[ReviewPerspective, ...] = (
    ReviewPerspective("methodology", REVIEW_METHODOLOGY_SYSTEM_PROMPT, needs_retrieval=False),
    ReviewPerspective("statistics", REVIEW_STATISTICS_SYSTEM_PROMPT, needs_retrieval=False),
    ReviewPerspective(
        "domain_consistency", REVIEW_DOMAIN_CONSISTENCY_SYSTEM_PROMPT, needs_retrieval=True
    ),
)
"""视角顺序是稳定的：hard_gate 按此顺序扫描以确定 blocking_factor，保证同样输入下判定确定。"""


# ====== Agent 工厂 ======

def build_domain_consistency_agent(
    model: str, tools: ToolRegistry, *, client: "AsyncOpenAI | None" = None,
) -> Agent:
    """构造配好 REVIEW_DOMAIN_CONSISTENCY_SYSTEM_PROMPT 的 Agent，供 domain_consistency 视角使用。

    对标 build_gap_miner_agent / build_novelty_agent：工厂存在的唯一理由就是保证绑对
    system prompt，少了它绑错 prompt 没有任何测试拦得住。

    Example:
        >>> agent = build_domain_consistency_agent("gpt-4o-mini", tools)  # doctest: +SKIP
        >>> agent.name  # doctest: +SKIP
        'domain_consistency_reviewer'
    """
    return create_agent(
        model=model, tools=tools, system_prompt=REVIEW_DOMAIN_CONSISTENCY_SYSTEM_PROMPT,
        client=client, name="domain_consistency_reviewer",
    )


# ====== 单视角审阅 ======

def format_premise_lines(package: HypothesisPackage) -> str:
    """把 supported_premises 格式化成逐行文本，供审阅 prompt 与修订 prompt 共用。

    两处调用点（build_review_prompt、revision.build_revision_prompt）原本各自内联同一段
    格式化逻辑；提出来是为了不让第三份拷贝（Task 8 的辩论 prompt）出现，而不是改变行为。

    Example:
        >>> format_premise_lines(package)  # doctest: +SKIP
        '- [supported_premise] X correlates with Y (refs: ev-0)'
    """
    return "\n".join(
        f"- [{premise.role.value}] {premise.claim} (refs: {', '.join(premise.supporting_refs) or '-'})"
        for premise in package.supported_premises
    ) or "(no supported premises)"


def build_review_prompt(package: HypothesisPackage, perspective: ReviewPerspective) -> str:
    """拼装单视角审阅 prompt。刻意只取 novel_hypothesis / supported_premises /
    predicted_observations / disconfirming_observations —— **不传 sampling_probability**，
    避免审阅侧锚定生成侧的自评概率（Co-Scientist）。

    Example:
        >>> build_review_prompt(package, REVIEW_PERSPECTIVES[0]).startswith("You are")  # doctest: +SKIP
        True
    """
    premise_lines = format_premise_lines(package)
    return "\n\n".join([
        perspective.system_prompt,
        REVIEW_PERSPECTIVE_HEADER_TEMPLATE.format(perspective_id=perspective.perspective_id)
        + SKEPTIC_REVIEW_USER_PROMPT_TEMPLATE.format(
            novel_hypothesis=package.novel_hypothesis,
            supported_premises=premise_lines,
            predicted_observations="\n".join(f"- {o}" for o in package.predicted_observations),
            disconfirming_observations="\n".join(
                f"- {o}" for o in package.disconfirming_observations
            ),
        ),
    ])


async def read_prior_transcript(
    novelty: NoveltyEvidenceReport, artifacts: ArtifactStore
) -> str:
    """取回 [5] 的检索转录供 domain_consistency 复用；取不到就返回空串退回完整检索。

    转录复用是优化不是前置条件：query_log_ref 为 None（novelty 失败降级），或 ref 存在但
    artifact 已缺失/损坏，都退回空串而不是让本视角失败。

    Example:
        >>> await read_prior_transcript(novelty, store)  # doctest: +SKIP
        'earlier retrieval notes'
    """
    if not novelty.query_log_ref:
        return ""
    try:
        return await artifacts.get_text(novelty.query_log_ref)
    except (ArtifactNotFoundError, ArtifactIntegrityError):
        return ""


def build_perspective_input(
    package: HypothesisPackage,
    perspective: ReviewPerspective,
    *,
    corpus_ref: ArtifactRef,
    prior_transcript: str = "",
) -> str:
    """一个视角实际消费的输入：非检索视角是单轮审阅 prompt，检索视角是给 Agent 的检索提问。

    **这是视角输入的唯一构造点。** review_one_perspective 与 revision 的 staleness 判定都调
    它——两侧分头拼装会让指纹永远失配、所有报告恒 stale，且没有任何测试会红。

    Example:
        >>> build_perspective_input(package, REVIEW_PERSPECTIVES[0],
        ...     corpus_ref="sha256:" + "c" * 64).startswith("You are")  # doctest: +SKIP
        True
    """
    if not perspective.needs_retrieval:
        return build_review_prompt(package, perspective)
    return DOMAIN_CONSISTENCY_QUESTION_TEMPLATE.format(
        novel_hypothesis=package.novel_hypothesis,
        corpus_ref=corpus_ref,
        prior_retrieval=prior_transcript or "(none; search from scratch)",
    )


async def review_one_perspective(
    package: HypothesisPackage,
    perspective: ReviewPerspective,
    *,
    novelty: NoveltyEvidenceReport,
    domain_review_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    llm_sem: asyncio.Semaphore | None = None,
    retrieval_sem: asyncio.Semaphore | None = None,
    model: Model | str | None = None,
) -> SkepticReport:
    """跑一个视角的审阅。needs_retrieval=False 走单轮调用；True 走两阶段（Agent 检索循环 →
    single_turn_chat 转结构化），并复用 [5] 的检索转录作起始上下文。

    转录复用是优化不是前置条件：query_log_ref 为 None（novelty 失败降级），或 ref 存在但
    artifact 已缺失/损坏，都会退回完整检索而不是让本视角失败。llm_sem/retrieval_sem 为 None
    时不限流；两次 limited_by 是先后而非嵌套，不跨候选内 novelty -> domain_consistency 的
    依赖边界持有名额。

    Example:
        >>> report = await review_one_perspective(package, REVIEW_PERSPECTIVES[0],
        ...     novelty=novelty, domain_review_agent=agent, artifacts=store,
        ...     corpus_ref=ref)  # doctest: +SKIP
        >>> report.perspective  # doctest: +SKIP
        'methodology'
    """
    if not perspective.needs_retrieval:
        prompt = build_perspective_input(package, perspective, corpus_ref=corpus_ref)
        async with limited_by(llm_sem):
            judgment = await single_turn_chat(prompt, SkepticJudgment, model=model)
        return SkepticReport(
            idea_id=package.idea_id, perspective=perspective.perspective_id,
            critique=judgment.critique, unaddressed_risks=judgment.unaddressed_risks,
            fatal_flaw_found=judgment.fatal_flaw_found,
            input_ref=await artifacts.put_text(prompt),
        )

    prior_transcript = await read_prior_transcript(novelty, artifacts)
    question = build_perspective_input(
        package, perspective, corpus_ref=corpus_ref, prior_transcript=prior_transcript
    )
    async with limited_by(retrieval_sem):
        collected_text, _channels = await run_retrieval_agent(domain_review_agent, question)
    transcript_ref = await artifacts.put_text(collected_text or "(agent produced no text)")
    async with limited_by(llm_sem):
        judgment = await single_turn_chat(
            DOMAIN_CONSISTENCY_SUMMARY_PROMPT_TEMPLATE.format(analysis=collected_text),
            SkepticJudgment, model=model,
        )
    return SkepticReport(
        idea_id=package.idea_id, perspective=perspective.perspective_id,
        critique=judgment.critique, unaddressed_risks=judgment.unaddressed_risks,
        fatal_flaw_found=judgment.fatal_flaw_found, transcript_ref=transcript_ref,
        input_ref=await artifacts.put_text(question),
    )


async def _review_with_retry(
    package: HypothesisPackage, perspective: ReviewPerspective, **kwargs
) -> SkepticReport:
    """最多尝试 MAX_REVIEW_ATTEMPTS 次（即重试 1 次）仍失败才标 failed=True。

    Example:
        >>> report = await _review_with_retry(package, REVIEW_PERSPECTIVES[0], novelty=novelty,
        ...     domain_review_agent=agent, artifacts=store, corpus_ref=ref)  # doctest: +SKIP
        >>> report.failed  # doctest: +SKIP
        False
    """
    last_error: Exception | None = None
    for _ in range(MAX_REVIEW_ATTEMPTS):
        try:
            return await review_one_perspective(package, perspective, **kwargs)
        except Exception as error:  # noqa: BLE001 - provider 报错形态不定，重试后再降级
            last_error = error
    return SkepticReport(
        idea_id=package.idea_id, perspective=perspective.perspective_id,
        critique=f"review failed after {MAX_REVIEW_ATTEMPTS} attempts: {last_error}",
        unaddressed_risks=[], fatal_flaw_found=False, failed=True,
    )


# ====== 审阅委员会 ======

async def review_board(
    package: HypothesisPackage,
    novelty: NoveltyEvidenceReport,
    *,
    domain_review_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    llm_sem: asyncio.Semaphore | None = None,
    retrieval_sem: asyncio.Semaphore | None = None,
    model: Model | str | None = None,
) -> list[SkepticReport]:
    """对一个候选跑齐 REVIEW_PERSPECTIVES 的全部视角，返回与常量同序的报告列表。三个视角并发
    执行（asyncio.gather 保序），并发度受 llm_sem/retrieval_sem 约束；每个视角内部先按
    MAX_REVIEW_ATTEMPTS 重试一次瞬时失败。

    fail-closed：单个视角重试用尽仍失败，映射成 failed=True 的报告而不是抛出——审阅没跑成
    不能等于审阅通过，判定由 hard_gate 按 failed 标志作出。外层 gather 仍保留
    return_exceptions=True 作最后防线（例如意外的非预期异常），因为 _review_with_retry
    已经吞掉了所有 review_one_perspective 抛出的异常，正常情况下不会再有异常穿透到这里。

    Example:
        >>> reports = await review_board(package, novelty, domain_review_agent=agent,
        ...     artifacts=store, corpus_ref=ref)  # doctest: +SKIP
        >>> [r.perspective for r in reports]  # doctest: +SKIP
        ['methodology', 'statistics', 'domain_consistency']
    """
    outcomes = await asyncio.gather(*[
        _review_with_retry(
            package, perspective, novelty=novelty,
            domain_review_agent=domain_review_agent, artifacts=artifacts,
            corpus_ref=corpus_ref, llm_sem=llm_sem, retrieval_sem=retrieval_sem,
            model=model,
        )
        for perspective in REVIEW_PERSPECTIVES
    ], return_exceptions=True)

    # gather 保序：outcomes[i] 一定对应 REVIEW_PERSPECTIVES[i]
    return [
        outcome if isinstance(outcome, SkepticReport) else SkepticReport(
            idea_id=package.idea_id, perspective=perspective.perspective_id,
            critique=f"review failed: {outcome}", unaddressed_risks=[],
            fatal_flaw_found=False, failed=True,
        )
        for perspective, outcome in zip(REVIEW_PERSPECTIVES, outcomes)
    ]

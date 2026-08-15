"""ResearchGapMiner（空白挖掘，步骤 [2]）与 NoveltyEvidenceCollector（数值性/时间完整性审计，
步骤 [5]）：两者都是"Agent+paper_rag 工具"两阶段模式——阶段一用配了 paper_rag 三个工具的
core.agent.Agent 跑一段开放式检索,阶段二用 single_turn_structured_chat 把自由文本
结论转成结构化 schema。thread_id/turn_id 用本地生成的 uuid 即可,不需要 app_server 的
ThreadManager 持久化。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from athena.core.agent import Agent, AgentContext, create_agent
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import TOOL_BEGIN
from athena.research.idea_generation.prompts import (
    GAP_MINER_QUESTION_TEMPLATE,
    GAP_MINER_SUMMARY_PROMPT_TEMPLATE,
    GAP_MINER_SYSTEM_PROMPT,
    NOVELTY_QUESTION_TEMPLATE,
    NOVELTY_SUMMARY_PROMPT_TEMPLATE,
    NOVELTY_SYSTEM_PROMPT,
)
from athena.research.idea_generation.idea_schemas import (
    GapCandidate,
    GapMiningResponse,
    HypothesisPackage,
    NoveltyEvidenceJudgment,
    NoveltyEvidenceReport,
    ResearchProblemInput,
    RetrievalCoverage,
    TemporalIntegrity,
)
from athena.research.idea_generation.structured_chat import single_turn_structured_chat

if TYPE_CHECKING:
    from openai import AsyncOpenAI


# ====== 常量 ======

DEFAULT_NOVELTY_TOP_K: int = 5


# ====== 限流辅助 ======

@asynccontextmanager
async def limited_by(semaphore: asyncio.Semaphore | None) -> AsyncIterator[None]:
    """semaphore 为 None 时不限流，否则用 async with 获取名额。

    Example:
        >>> async with limited_by(None):  # doctest: +SKIP
        ...     pass
    """
    if semaphore is None:
        yield
        return
    async with semaphore:
        yield


# ====== 输入构造（staleness 判定与生产侧共用，禁止两侧分头拼装） ======

def build_novelty_question(package: HypothesisPackage, corpus_ref: ArtifactRef) -> str:
    """构造 [5] 数值性审计的检索提问。它同时是该报告的 staleness 指纹来源。

    ``package.sources``（生成侧真正读过的论文）随提问带下来，让审计把预算花在"生成侧
    漏了什么"而不是重新发现同一批文献——与 review_board 把 novelty 转录交给
    domain_consistency 是同一条接力，只是再往上游延了一段。它进入指纹是正确的：
    生成侧读过什么变了，缓存的旧 novelty 报告就该失效。

    Example:
        >>> build_novelty_question(package, "sha256:" + "a" * 64).startswith("Hypothesis under review:")  # doctest: +SKIP
        True
    """
    return NOVELTY_QUESTION_TEMPLATE.format(
        novel_hypothesis=package.novel_hypothesis,
        corpus_ref=corpus_ref,
        predicted_observations="\n".join(f"- {o}" for o in package.predicted_observations),
        prior_sources="\n".join(f"- {s}" for s in package.sources)
                      or "(none; the generator had no corpus - search from scratch)",
    )


# ====== 阶段一共用：Agent 检索循环 ======

async def run_retrieval_agent(agent: Agent, question: str) -> tuple[str, list[str]]:
    """两阶段模式的阶段一：跑一段开放式 Agent 检索循环，返回 (自由文本结论, 实际调用过的工具名)。

    Example:
        >>> text, channels = await run_retrieval_agent(agent, "question")  # doctest: +SKIP
    """
    collected: list[str] = []
    channels: list[str] = []

    async def emit(kind: str, event_ref: str, data: dict | None = None) -> None:
        if kind == "agent/text_delta" and data:
            collected.append(data.get("delta", ""))
        elif kind == TOOL_BEGIN:
            parts = event_ref.split(":")
            if len(parts) >= 2:
                channels.append(parts[-2])

    thread_id = f"thread-{uuid.uuid4().hex[:8]}"
    turn_id = f"turn-{uuid.uuid4().hex[:8]}"
    # AgentContext.tools 只在手动驱动的 Agent（调用 self.tool(ctx, name, ...)）里有用；
    # core.agent.Agent 自身的 ReAct 循环按 agent.tools 解析工具调用，这里直接复用 agent.tools。
    ctx = AgentContext(
        thread=AthenaThread(thread_id=thread_id, session_id="idea-generation", status="running",
                             context_ref="context://idea-generation"),
        turn=AthenaTurn(turn_id=turn_id, thread_id=thread_id, request_ref=question, status="running"),
        emit=emit,
        tools=agent.tools,
        cancel=asyncio.Event(),
    )
    await agent.run(ctx)
    return "".join(collected), channels


# ====== Agent 工厂：把两段 system_prompt 绑到可直接使用的 Agent 上 ======

def build_gap_miner_agent(
    model: str, tools: ToolRegistry, *, client: "AsyncOpenAI | None" = None,
) -> Agent:
    """构造配好 GAP_MINER_SYSTEM_PROMPT 的 Agent，供 mine_research_gaps 使用。

    Example:
        >>> agent = build_gap_miner_agent("gpt-4o-mini", tools, client=client)  # doctest: +SKIP
    """
    return create_agent(
        model=model, tools=tools, system_prompt=GAP_MINER_SYSTEM_PROMPT, client=client,
        name="research_gap_miner",
    )


def build_novelty_agent(
    model: str, tools: ToolRegistry, *, client: "AsyncOpenAI | None" = None,
) -> Agent:
    """构造配好 NOVELTY_SYSTEM_PROMPT 的 Agent，供 collect_novelty_evidence 使用。

    Example:
        >>> agent = build_novelty_agent("gpt-4o-mini", tools, client=client)  # doctest: +SKIP
    """
    return create_agent(
        model=model, tools=tools, system_prompt=NOVELTY_SYSTEM_PROMPT, client=client,
        name="novelty_evidence_collector",
    )


# ====== ResearchGapMiner（步骤 [2]） ======

async def mine_research_gaps(
    problem: ResearchProblemInput,
    *,
    agent: Agent,
    corpus_ref: ArtifactRef,
    artifacts: ArtifactStore,
    model: str,
) -> list[GapCandidate]:
    """迭代检索定位 unsolved/contradiction/missing-link 三类空白；空列表代表没找到空白，
    不是报错。gap_id/context_ref 由代码分配，不向 LLM 索要。

    Example:
        >>> gaps = await mine_research_gaps(problem, agent=agent, corpus_ref=corpus_ref,
        ...     artifacts=store, model="m")  # doctest: +SKIP
        >>> isinstance(gaps, list)
        True
    """
    question = GAP_MINER_QUESTION_TEMPLATE.format(
        question=problem.question, domain=problem.domain, objective=problem.objective,
        corpus_ref=corpus_ref,
    )
    collected_text, _channels = await run_retrieval_agent(agent, question)
    context_ref = await artifacts.put_text(collected_text or "(agent produced no text)")

    response = await single_turn_structured_chat(
        GAP_MINER_SUMMARY_PROMPT_TEMPLATE.format(analysis=collected_text),
        GapMiningResponse, model=model, artifacts=artifacts,
    )
    return [
        GapCandidate(
            gap_id=f"gap-{uuid.uuid4().hex[:12]}", gap_type=draft.gap_type,
            context_ref=context_ref, description=draft.description,
        )
        for draft in response.gaps
    ]


# ====== 降级报告（检索失败时的空报告，两处调用点共用） ======

async def degraded_novelty_report(
    idea_id: str, error: Exception, artifacts: ArtifactStore
) -> NoveltyEvidenceReport:
    """novelty 检索失败时的降级空报告。

    Example:
        >>> report = await degraded_novelty_report(
        ...     "idea-1", RuntimeError("down"), store)  # doctest: +SKIP
        >>> report.facet_overlap
        {}
    """
    empty_ref = await artifacts.put_text(f"novelty retrieval failed: {error}")
    return NoveltyEvidenceReport(
        idea_id=idea_id, nearest_work=[], facet_overlap={},
        coverage_ref=empty_ref, temporal_ref=empty_ref, query_log_ref=None,
        uncertainty=1.0,
    )


# ====== NoveltyEvidenceCollector（步骤 [5]） ======

async def collect_novelty_evidence(
    package: HypothesisPackage,
    *,
    agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    top_k: int = DEFAULT_NOVELTY_TOP_K,
    model_training_cutoff: str | None = None,
    llm_sem: asyncio.Semaphore | None = None,
    retrieval_sem: asyncio.Semaphore | None = None,
    model: str,
) -> NoveltyEvidenceReport:
    """检索最相近工作，产出数值性 facet_overlap 打分与 TemporalIntegrity 泄漏风险；不产出
    verdict，判断权留给 hard_gate。

    Example:
        >>> report = await collect_novelty_evidence(package, agent=agent,
        ...     artifacts=store, corpus_ref=corpus_ref, model="m")  # doctest: +SKIP
    """
    question = build_novelty_question(package, corpus_ref)
    async with limited_by(retrieval_sem):
        collected_text, channels_used = await run_retrieval_agent(agent, question)
    query_log_ref = await artifacts.put_text(collected_text or "(agent produced no text)")

    async with limited_by(llm_sem):
        judgment = await single_turn_structured_chat(
            NOVELTY_SUMMARY_PROMPT_TEMPLATE.format(analysis=collected_text),
            NoveltyEvidenceJudgment, model=model, artifacts=artifacts,
        )

    coverage = RetrievalCoverage(
        channels_used=sorted(set(channels_used)),
        retrieval_ceiling=top_k,
        decision_set_size=len(judgment.nearest_work),
        display_top_k=top_k,
        coverage_estimate=judgment.coverage_estimate,
        unrecalled_risk=judgment.unrecalled_risk,
        index_version=corpus_ref,
        query_log_ref=query_log_ref,
    )
    temporal = TemporalIntegrity(
        citation_cutoff_ok=judgment.citation_cutoff_ok,
        retrieval_cutoff_ok=judgment.retrieval_cutoff_ok,
        index_snapshot_time=datetime.now(timezone.utc).isoformat(),
        model_training_cutoff_known=model_training_cutoff,
        post_cutoff_similarity=judgment.post_cutoff_similarity,
        possible_memorization=judgment.possible_memorization,
        leakage_risk=judgment.leakage_risk,
        historical_backtest_validity=judgment.historical_backtest_validity,
    )
    coverage_ref = await artifacts.put_text(coverage.model_dump_json())
    temporal_ref = await artifacts.put_text(temporal.model_dump_json())

    return NoveltyEvidenceReport(
        idea_id=package.idea_id,
        nearest_work=judgment.nearest_work,
        facet_overlap=judgment.facet_overlap,
        coverage_ref=coverage_ref,
        temporal_ref=temporal_ref,
        query_log_ref=query_log_ref,
        input_ref=await artifacts.put_text(question),
        uncertainty=judgment.uncertainty,
    )

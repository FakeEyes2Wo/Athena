"""ResearchGapMiner（空白挖掘，步骤 [2]）与 NoveltyEvidenceCollector（数值性/时间完整性审计，
步骤 [5]）：两者都是"Agent+paper_rag 工具"两阶段模式（设计文档第8节/9.2）——阶段一用配了
paper_rag 三个工具的 core.agent.Agent 跑一段开放式检索,阶段二用 single_turn_chat 把自由文本
结论转成结构化 schema。thread_id/turn_id 用本地生成的 uuid 即可,不需要 app_server 的
ThreadManager 持久化。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic_ai.models import Model

from athena.core.agent import Agent, AgentContext, create_agent
from athena.core.schemas import ArtifactRef, AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import TOOL_BEGIN
from athena.storage.artifact_store import ArtifactStore
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    GAP_MINER_QUESTION_TEMPLATE,
    GAP_MINER_SUMMARY_PROMPT_TEMPLATE,
    GAP_MINER_SYSTEM_PROMPT,
    NOVELTY_QUESTION_TEMPLATE,
    NOVELTY_SUMMARY_PROMPT_TEMPLATE,
    NOVELTY_SYSTEM_PROMPT,
)
from athena.workflows.search.idea_schemas import (
    GapCandidate,
    GapMiningResponse,
    HypothesisPackage,
    NoveltyEvidenceJudgment,
    NoveltyEvidenceReport,
    ResearchProblemInput,
    RetrievalCoverage,
    TemporalIntegrity,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


# ====== 常量 ======

DEFAULT_NOVELTY_TOP_K: int = 5


# ====== 限流辅助 ======

@asynccontextmanager
async def limited_by(semaphore: asyncio.Semaphore | None) -> AsyncIterator[None]:
    """semaphore 为 None 时不限流，否则用 async with 获取名额。

    只用 async with、不手动 acquire()/release()——异常路径漏 release 会静默泄漏名额，
    症状是流水线越跑越慢直至卡死，极难定位。

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
    """构造 [5] 数值性审计的检索提问。它同时是该报告的 staleness 指纹来源，所以
    collect_novelty_evidence 与 revision.novelty_is_stale 必须都调这一个函数——两侧分头
    拼装会让指纹永远失配、所有报告恒 stale。

    Example:
        >>> build_novelty_question(package, "sha256:" + "a" * 64).startswith("Hypothesis under review:")  # doctest: +SKIP
        True
    """
    return NOVELTY_QUESTION_TEMPLATE.format(
        novel_hypothesis=package.novel_hypothesis,
        corpus_ref=corpus_ref,
        predicted_observations="\n".join(f"- {o}" for o in package.predicted_observations),
    )


# ====== 阶段一共用：Agent 检索循环 ======

async def run_retrieval_agent(agent: Agent, question: str) -> tuple[str, list[str]]:
    """两阶段模式的阶段一：跑一段开放式 Agent 检索循环，返回 (自由文本结论, 实际调用过的工具名)。

    ``AgentContext.tools`` 只在手动驱动的 Agent（调用 ``self.tool(ctx, name, ...)``）里有用；
    ``core.agent.Agent`` 自身的 ReAct 循环按 ``agent.config.tools`` 解析工具调用（见
    ``core/agent/agent.py`` 的 ``_sampling_loop``），所以这里直接复用 ``agent.config.tools``，
    不再让调用方多传一份可能对不上的 ToolRegistry。

    Example:
        >>> text, channels = await run_retrieval_agent(agent, "question")  # doctest: +SKIP
    """
    collected: list[str] = []
    channels: list[str] = []

    async def emit(kind: str, event_ref: str, data: dict | None = None) -> None:
        if kind == "agent/text_delta" and data:
            collected.append(data.get("delta", ""))
        elif kind == TOOL_BEGIN:
            # core/tool.py 用 f"ev:{ctx.call_id}:begin" 发事件，而 agent.py 把 call_id 拼成
            # f"{turn_id}:{tool_name}"，所以 event_ref 形如 "ev:{turn_id}:{tool_name}:begin"，
            # 工具名是倒数第二段
            parts = event_ref.split(":")
            if len(parts) >= 2:
                channels.append(parts[-2])

    thread_id = f"thread-{uuid.uuid4().hex[:8]}"
    turn_id = f"turn-{uuid.uuid4().hex[:8]}"
    ctx = AgentContext(
        thread=AthenaThread(thread_id=thread_id, session_id="idea-generation", status="running",
                             context_ref="context://idea-generation"),
        turn=AthenaTurn(turn_id=turn_id, thread_id=thread_id, request_ref=question, status="running"),
        emit=emit,
        tools=agent.config.tools,
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
        >>> agent.config.system_prompt == GAP_MINER_SYSTEM_PROMPT  # doctest: +SKIP
        True
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
        >>> agent.config.system_prompt == NOVELTY_SYSTEM_PROMPT  # doctest: +SKIP
        True
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
    model: Model | str | None = None,
) -> list[GapCandidate]:
    """迭代检索定位 unsolved/contradiction/missing-link 三类空白；空列表代表没找到空白，
    不是报错。gap_id/context_ref 由代码分配，不向 LLM 索要。agent 需已配好 paper_rag 工具
    (agent.config.tools)，corpus_ref 会写进问题文本，供 agent 原样传给 paper_rag 工具。

    Example:
        >>> gaps = await mine_research_gaps(problem, agent=agent, corpus_ref=corpus_ref,
        ...     artifacts=store)  # doctest: +SKIP
        >>> isinstance(gaps, list)
        True
    """
    question = GAP_MINER_QUESTION_TEMPLATE.format(
        question=problem.question, domain=problem.domain, objective=problem.objective,
        corpus_ref=corpus_ref,
    )
    collected_text, _channels = await run_retrieval_agent(agent, question)
    context_ref = await artifacts.put_text(collected_text or "(agent produced no text)")

    response = await single_turn_chat(
        GAP_MINER_SUMMARY_PROMPT_TEMPLATE.format(analysis=collected_text), GapMiningResponse, model=model,
    )
    return [
        GapCandidate(
            gap_id=f"gap-{uuid.uuid4().hex[:12]}", gap_type=draft.gap_type,
            context_ref=context_ref, description=draft.description,
        )
        for draft in response.gaps
    ]


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
    model: Model | str | None = None,
) -> NoveltyEvidenceReport:
    """检索最相近工作，产出数值性 facet_overlap 打分与 TemporalIntegrity 泄漏风险；不产出
    verdict，判断权留给 hard_gate。channels_used/index_version/query_log_ref 等确定性事实
    由代码填，facet_overlap/coverage_estimate 等解释性判断由 LLM 填。agent 需已配好 paper_rag
    工具(agent.config.tools)，corpus_ref 既写进问题文本供 agent 原样传给工具，也用作
    RetrievalCoverage.index_version。llm_sem/retrieval_sem 为 None 时不限流,供并发流水线
    传入 workflow.LLM_CONCURRENCY / RETRIEVAL_CONCURRENCY 控制的信号量。

    Example:
        >>> report = await collect_novelty_evidence(package, agent=agent,
        ...     artifacts=store, corpus_ref=corpus_ref)  # doctest: +SKIP
        >>> report.uncertainty  # doctest: +SKIP
        0.2
    """
    question = build_novelty_question(package, corpus_ref)
    async with limited_by(retrieval_sem):
        collected_text, channels_used = await run_retrieval_agent(agent, question)
    query_log_ref = await artifacts.put_text(collected_text or "(agent produced no text)")

    async with limited_by(llm_sem):
        judgment = await single_turn_chat(
            NOVELTY_SUMMARY_PROMPT_TEMPLATE.format(analysis=collected_text), NoveltyEvidenceJudgment, model=model,
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
        uncertainty=judgment.uncertainty,
    )

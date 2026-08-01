"""PaperScout Agent：把论文检索当作多轮决策过程来执行。

与 Athena 通用 ``Agent`` 的关键区别是没有对话历史。PaperScout 的状态全部由 paper pool
承载，每一步都用当前 pool 的观测重新构造一次两条消息的提示；模型看到的是"现在池里有
什么、已经做过哪些动作"，而不是一条越来越长的消息链。这既是论文的 POMDP 形式化本身，
也让上下文长度不随步数增长。

停止条件按论文：池连续三步没有变化就终止；此外受最大步数和墙钟预算约束。
"""

import asyncio
import time

from openai import AsyncOpenAI
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.core.agent.agent import AgentConfig, AgentContext, AgentOutcome, BaseAgent
from athena.core.agent.provider import ResponsesProvider
from athena.core.tool import ToolRegistry
from athena.research.paper_scout.backends import ReferenceBackend, SearchBackend
from athena.research.paper_scout.prompts import (
    PAPERSCOUT_SYSTEM_PROMPT,
    PAPERSCOUT_USER_PROMPT,
    format_history,
)
from athena.research.paper_scout.schemas import (
    IDLE_TURNS_BEFORE_STOP,
    RETAIN_THRESHOLD,
    PaperScoutResult,
    ScoutCorpus,
    ScoutRequest,
    ScoutStats,
)
from athena.research.paper_scout.scorer import RelevanceScorer
from athena.research.paper_scout.session import ScoutSession
from athena.research.paper_scout.tool import (
    EXPAND_TOOL_NAME,
    SEARCH_TOOL_NAME,
    PaperScoutExpandTool,
    PaperScoutSearchTool,
)
from athena.storage.artifact_store import ArtifactStore

POLICY_MAX_TOKENS = 2048
POLICY_TEMPERATURE = 0.3
ACTION_TOOLS = (SEARCH_TOOL_NAME, EXPAND_TOOL_NAME)


async def collect_tool_calls(
    provider: ResponsesProvider,
    config: AgentConfig,
    prompt: str,
    cancel: asyncio.Event,
) -> tuple[list[tuple[str, str, dict]], str]:
    """跑一次策略采样，收集它发出的工具调用与分析文本。

    返回的调用形如 ``[(tool_name, call_id, arguments)]``，可直接交给
    ``ToolRegistry.adispatch``。
    """
    messages = [ModelRequest(parts=[UserPromptPart(content=prompt)])]
    calls: list[tuple[str, str, dict]] = []
    analysis = ""
    async for event in provider.stream(config, messages, cancel):
        if event.kind == "text_delta":
            analysis = event.data.get("accumulated", analysis)
        elif event.kind == "function_call":
            name = event.data.get("name", "")
            if name in ACTION_TOOLS:
                calls.append(
                    (
                        name,
                        event.data.get("call_id", ""),
                        event.data.get("arguments", {}),
                    )
                )
        elif event.kind == "error":
            raise RuntimeError(event.data.get("message", "policy stream failed"))
    return calls, analysis


class PaperScoutAgent(BaseAgent):
    """按 PaperScout 的 search/expand 决策过程收集论文的 Athena Agent。

    依赖全部由组合根注入：检索后端、引用后端、相关性 scorer 和模型客户端。模块不在导入
    时创建客户端，也不读环境变量。
    """

    name = "paper_scout"
    description = "Multi-turn academic paper search with search/expand actions."

    def __init__(
        self,
        artifacts: ArtifactStore,
        search_backends: list[SearchBackend],
        reference_backend: ReferenceBackend | None,
        scorer: RelevanceScorer,
        *,
        model: str,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.search_backends = search_backends
        self.reference_backend = reference_backend
        self.scorer = scorer
        self.model = model
        self._provider = ResponsesProvider(client=client)

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """执行一次完整的 PaperScout 检索并落盘结果。"""
        request = ScoutRequest.model_validate_json(
            await self.artifacts.get_text(ctx.turn.request_ref)
        )
        session = ScoutSession(
            request, self.search_backends, self.reference_backend, self.scorer
        )
        tools = ToolRegistry()
        tools.register(PaperScoutSearchTool(session))
        tools.register(PaperScoutExpandTool(session))
        config = AgentConfig(
            model=self.model,
            system_prompt=PAPERSCOUT_SYSTEM_PROMPT,
            tools=tools,
            max_tokens=POLICY_MAX_TOKENS,
            temperature=POLICY_TEMPERATURE,
        )
        await ctx.emit(
            "paper_scout/started",
            ctx.turn.request_ref,
            {"query": request.query, "model": self.model},
        )

        started = time.monotonic()
        stats = ScoutStats()
        stop_reason = await self._loop(ctx, session, config, stats, started)
        stats.steps = session.step
        stats.wall_seconds = round(time.monotonic() - started, 3)
        stats.stop_reason = stop_reason
        return await self._finish(ctx, session, stats)

    async def _loop(
        self,
        ctx: AgentContext,
        session: ScoutSession,
        config: AgentConfig,
        stats: ScoutStats,
        started: float,
    ) -> str:
        """驱动多轮决策，返回停止原因。"""
        idle_turns = 0
        for step in range(1, session.request.max_steps + 1):
            if ctx.cancel.is_set():
                return "cancelled"
            if time.monotonic() - started >= session.request.max_seconds:
                return "max_seconds"
            session.step = step
            prompt = PAPERSCOUT_USER_PROMPT.format(
                user_query=session.request.query,
                history_actions=format_history(session.history),
                paper_list=session.pool.observation(),
            )
            stats.policy_calls += 1
            calls, analysis = await collect_tool_calls(
                self._provider, config, prompt, ctx.cancel
            )
            if not calls:
                return "policy_returned_no_action"

            before = len(session.pool)
            await config.tools.adispatch(
                [
                    (name, call_id or f"{ctx.turn.turn_id}:{step}:{index}", arguments)
                    for index, (name, call_id, arguments) in enumerate(
                        calls[: session.request.max_parallel_calls]
                    )
                ],
                ctx.emit,
                ctx.cancel,
            )
            await ctx.emit(
                "paper_scout/step",
                ctx.turn.request_ref,
                {
                    "step": step,
                    "calls": len(calls),
                    "pool": len(session.pool),
                    "analysis": analysis[:500],
                },
            )
            idle_turns = idle_turns + 1 if len(session.pool) == before else 0
            if idle_turns >= IDLE_TURNS_BEFORE_STOP:
                return "pool_unchanged"
        return "max_steps"

    async def _finish(
        self, ctx: AgentContext, session: ScoutSession, stats: ScoutStats
    ) -> AgentOutcome:
        """汇总统计、写入 artifact 并返回 Turn 结果。"""
        retained = session.pool.retained(RETAIN_THRESHOLD, session.request.max_papers)
        stats.search_actions = sum(
            1 for action in session.actions if action.kind == "search"
        )
        stats.expand_actions = sum(
            1 for action in session.actions if action.kind == "expand"
        )
        stats.repeated_actions = sum(1 for action in session.actions if action.repeated)
        stats.pool_size = len(session.pool)
        stats.scored_papers = len(session.pool)
        stats.retained_papers = len(retained)
        stats.scorer_calls = getattr(session.scorer, "calls", 0)
        stats.backend_requests = self._backend_requests()
        stats.errors = session.errors[:50]

        stats_ref = await self.artifacts.put_text(stats.model_dump_json())
        corpus = ScoutCorpus(
            query=session.request.query,
            retained=retained,
            pool=session.pool.ranked(),
            actions=session.actions,
            stats_ref=stats_ref,
        )
        corpus_ref = await self.artifacts.put_text(corpus.model_dump_json())
        status = "partial" if session.errors else "complete"
        result = PaperScoutResult(
            status=status,
            corpus_ref=corpus_ref,
            stats_ref=stats_ref,
            paper_count=len(retained),
            warnings=sorted({error.split(":")[0] for error in session.errors}),
        )
        result_ref = await self.artifacts.put_text(result.model_dump_json())
        await ctx.emit(
            "paper_scout/completed",
            result_ref,
            {"papers": len(retained), "stop_reason": stats.stop_reason},
        )
        return AgentOutcome(
            result_ref=result_ref,
            next_context_ref=f"context://{ctx.turn.turn_id}/next",
        )

    def _backend_requests(self) -> int:
        """汇总各后端共享限流器的真实请求数；限流器缺失时返回 0。"""
        limiters = {
            id(backend.http): backend.http
            for backend in self.search_backends
            if hasattr(backend, "http")
        }
        return sum(getattr(item, "request_count", 0) for item in limiters.values())

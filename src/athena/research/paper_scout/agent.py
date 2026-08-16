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
from pydantic import ValidationError
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

from athena.core.agent.models import AgentConfig, AgentContext, AgentOutcome
from athena.core.agent.provider import BaseProvider, ResponsesProvider
from athena.core.agent.runtime import BaseAgent
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult
from athena.research.paper_scout.backends import ReferenceBackend, SearchBackend
from athena.research.paper_scout.pool import has_retrievable_source
from athena.research.paper_scout.prompts import (
    PAPERSCOUT_SYSTEM_PROMPT,
    PAPERSCOUT_USER_PROMPT,
    format_history,
)
from athena.research.paper_scout.schemas import (
    IDLE_TURNS_BEFORE_STOP,
    PaperScoutResult,
    ScoutCorpus,
    ScoutRequest,
    ScoutStats,
)
from athena.research.paper_scout.scorer import RelevanceScorer
from athena.research.paper_scout.selection import BoundarySelector, select_delivery
from athena.research.paper_scout.session import ScoutSession
from athena.research.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourceRequest,
    SourceHint,
)
from athena.research.paper_scout.tool import (
    EXPAND_TOOL_NAME,
    SEARCH_TOOL_NAME,
    PaperScoutExpandTool,
    PaperScoutSearchTool,
)

POLICY_MAX_TOKENS = 2048
POLICY_TEMPERATURE = 0.3
ACTION_TOOLS = (SEARCH_TOOL_NAME, EXPAND_TOOL_NAME)


async def collect_tool_calls(
    provider: BaseProvider,
    config: AgentConfig,
    tools: ToolRegistry,
    prompt: str,
    cancel: asyncio.Event,
) -> tuple[list[tuple[str, str, dict]], str]:
    """跑一次策略采样，收集它发出的工具调用与分析文本。

    每次采样都从这两条消息重新开始，不带上一步的历史：pool 观测已经是完整状态，
    重放历史只会让上下文随步数增长。返回的调用形如
    ``[(tool_name, call_id, arguments)]``，可直接交给 ``dispatch_tool_calls``。
    """
    messages = [
        ModelRequest(parts=[SystemPromptPart(content=PAPERSCOUT_SYSTEM_PROMPT)]),
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
    ]
    calls: list[tuple[str, str, dict]] = []
    analysis = ""
    async for event in provider.stream(config, tools, messages, cancel):
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


async def dispatch_tool_calls(
    tools: ToolRegistry,
    calls: list[tuple[str, str, dict]],
    emit,
    cancel: asyncio.Event,
) -> list[ToolResult | BaseException]:
    """并发执行一步内的全部 search/expand 调用。

    ``core/tool`` 的 ``ToolRegistry`` 只做注册与解析，派发由调用方负责。scout 的两个
    动作都只改写自己的 pool（``ScoutSession`` 内部加锁），因此可以整批并发。
    """
    return await asyncio.gather(
        *[
            tools.resolve(name).ainvoke(
                ToolContext(name, call_id, emit, cancel), **arguments
            )
            for name, call_id, arguments in calls
        ],
        return_exceptions=True,
    )


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
        selector: BoundarySelector | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.search_backends = search_backends
        self.reference_backend = reference_backend
        self.scorer = scorer
        self.model = model
        # 边界档重排器；缺省时选片回退到散列次序，行为与此前一致。
        self.selector = selector
        self._provider = ResponsesProvider(model, client=client)

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
            max_tokens=POLICY_MAX_TOKENS,
            temperature=POLICY_TEMPERATURE,
            name="paper-scout",
        )
        await ctx.emit(
            "paper_scout/started",
            ctx.turn.request_ref,
            {"query": request.query, "model": self.model},
        )

        started = time.monotonic()
        stats = ScoutStats()
        stop_reason = await self._loop(ctx, session, config, tools, stats, started)
        stats.steps = session.step
        stats.wall_seconds = round(time.monotonic() - started, 3)
        stats.stop_reason = stop_reason
        return await self._finish(ctx, session, stats)

    async def _loop(
        self,
        ctx: AgentContext,
        session: ScoutSession,
        config: AgentConfig,
        tools: ToolRegistry,
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
                self._provider, config, tools, prompt, ctx.cancel
            )
            if not calls:
                return "policy_returned_no_action"

            before = len(session.pool)
            await dispatch_tool_calls(
                tools,
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
        eligible = session.pool.retained(session.request.retain_threshold)
        # 截断前的完整候选：交付名额几乎总是落在某一档内部，而"该选哪几篇"不能由
        # tie_break 的散列决定（见 selection.select_delivery）。
        contenders = session.pool.retained(
            session.request.retain_threshold,
            require_retrievable_source=session.request.require_retrievable_source,
        )
        # 重排要对准**真正的交付量**，不是候选上限。取源按 3 倍超额下单、够数即停，所以
        # ``max_papers`` 是候选上限（真机 60），而实际进语料的是 ``stop_after_fetched``
        # （真机 20）。按候选上限切档，截断线落在第 60 位——真机实测那里是 197 篇同为
        # 0.20 的尾部，而取源试到第 36 篇就够数了，那一档一篇都没被碰过。也就是说重排
        # 精心排了一批永远不会被下载的论文。按交付量切档，截断线才落在真正有人争的地方。
        target = session.request.paper_source_policy.stop_after_fetched
        selection = await select_delivery(
            session.request.query,
            contenders,
            target or session.request.max_papers,
            self.selector,
        )
        # 选出的交付集合在前，其余候选按原次序垫在后面：取源逐个尝试、失败就往后走，
        # 垫底的存在意义就是接住取源失败，不该因为没被选中而消失。
        chosen = {item.paper_key for item in selection.delivered}
        backups = [item for item in contenders if item.paper_key not in chosen]
        retained = (selection.delivered + backups)[: session.request.max_papers or None]
        stats.boundary_tier = selection.boundary_size
        stats.boundary_reranked = selection.reranked
        stats.facets = list(selection.facets)
        stats.facet_coverage = round(selection.coverage(), 3)
        stats.selection_note = selection.note
        if session.request.require_retrievable_source:
            stats.dropped_no_source = sum(
                1 for paper in eligible if not has_retrievable_source(paper)
            )
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
        source_ref = await self._paper_source_request(
            retained, corpus_ref, session.request
        )
        status = "partial" if session.errors else "complete"
        result = PaperScoutResult(
            status=status,
            corpus_ref=corpus_ref,
            stats_ref=stats_ref,
            paper_source_request_ref=source_ref,
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

    async def _paper_source_request(
        self, retained: list, corpus_ref: str, request: ScoutRequest
    ) -> str | None:
        """把交付集合转成 paper_source 可直接消费的取源请求。

        只带真实标识符的论文才进得去：``PaperIdentity`` 要求至少一个非标题标识符，纯标题
        的候选交给下游只会变成无法解析的失败记录。全都没有标识符时返回 ``None``，与
        "零命中"区分开。顺序沿用交付顺序，即优先级。

        检索阶段拿到的开放获取链接顺带传成 ``SourceHint``：``paper_source`` 会优先试
        这些 URL，省掉一次 OpenAlex 解析，也避开了 OpenAlex 的 ``best_oa_location``
        指向第三方镜像的已知问题。线索不是权威，字节仍然要校验。
        """
        papers = []
        for paper in retained:
            if not (paper.arxiv_id or paper.doi or paper.s2_paper_id):
                continue
            try:
                identity = PaperIdentity(
                    arxiv_id=paper.arxiv_id or None,
                    doi=paper.doi or None,
                    s2_paper_id=paper.s2_paper_id or None,
                )
            except ValidationError:
                # 后端给出的标识符格式不合法 → 跳过这一篇，而不是让整个 Turn 失败
                continue
            hints = []
            if paper.open_access_pdf:
                hints.append(
                    SourceHint(
                        url=paper.open_access_pdf,
                        kind="oa_pdf",
                        channel=paper.channel,
                        is_open_access=paper.is_open_access,
                    )
                )
            papers.append(
                PaperRef(
                    identity=identity,
                    upstream_metadata={
                        "title": paper.title,
                        "year": str(paper.year) if paper.year else "",
                    },
                    hints=hints,
                    retrieval_channels=[paper.channel] if paper.channel else [],
                    matched_queries=[paper.origin] if paper.origin else [],
                )
            )
        if not papers:
            return None
        source_request = PaperSourceRequest(
            papers=papers, policy=request.paper_source_policy, corpus_ref=corpus_ref
        )
        return await self.artifacts.put_text(source_request.model_dump_json())

    def _backend_requests(self) -> int:
        """汇总各后端共享限流器的真实请求数；限流器缺失时返回 0。"""
        limiters = {
            id(backend.http): backend.http
            for backend in self.search_backends
            if hasattr(backend, "http")
        }
        return sum(getattr(item, "request_count", 0) for item in limiters.values())

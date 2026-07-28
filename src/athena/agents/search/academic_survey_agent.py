"""AcademicSurvey LangGraph 到 Athena BaseAgent 的薄适配层。"""

import asyncio
import hashlib
import time

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import NodeCancelledError

from athena.core.agent.agent import AgentContext, AgentOutcome, BaseAgent
from athena.research.academic_survey.chains import (
    DEFAULT_PROMPTS,
    LangChainSurveyChains,
)
from athena.research.academic_survey.budget import budget_for
from athena.research.academic_survey.cache import (
    CachedChannelAdapter,
    CachedSurveyChains,
    MemorySurveyCache,
    SurveyCache,
    reset_run_metrics,
    start_run_metrics,
)
from athena.research.academic_survey.graph import SurveyRuntime, build_survey_graph
from athena.research.academic_survey.interfaces import ChannelAdapter, SurveyChains
from athena.research.academic_survey.schemas import (
    ChannelName,
    PromptBundle,
    SurveyRequest,
)
from athena.storage.artifact_store import ArtifactStore


class AcademicSurveyAgent(BaseAgent):
    """运行一次受预算约束的论文发现任务。"""

    name = "academic_survey"
    description = "Find and rank a reference-paper corpus for paper_source."

    def __init__(
        self,
        artifacts: ArtifactStore,
        channels: dict[ChannelName, ChannelAdapter],
        *,
        model: BaseChatModel | None = None,
        chains: SurveyChains | None = None,
        prompts: PromptBundle = DEFAULT_PROMPTS,
        checkpointer: BaseCheckpointSaver | None = None,
        cache: SurveyCache | None = None,
        cache_max_age_seconds: float | None = None,
    ) -> None:
        if chains is None:
            if model is None:
                raise ValueError("model is required when chains are not injected")
            chains = LangChainSurveyChains(model, prompts)
        self.artifacts = artifacts
        self.cache = cache or MemorySurveyCache()
        self.channels = {
            name: (
                adapter
                if isinstance(adapter, CachedChannelAdapter)
                else CachedChannelAdapter(
                    adapter,
                    artifacts,
                    self.cache,
                    name=name,
                    max_age_seconds=cache_max_age_seconds,
                )
            )
            for name, adapter in channels.items()
        }
        self.chains = (
            chains
            if isinstance(chains, CachedSurveyChains)
            else CachedSurveyChains(chains, artifacts, self.cache)
        )
        self.graph = build_survey_graph(checkpointer)

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """执行图并只把顶层结果引用交回 Athena runtime。"""
        run_id = f"{ctx.thread.thread_id}:{ctx.turn.turn_id}"
        checkpoint_id = hashlib.sha256(run_id.encode()).hexdigest()
        request = SurveyRequest.model_validate_json(
            await self.artifacts.get_text(ctx.turn.request_ref)
        )
        deadline = time.monotonic() + budget_for(request.mode).max_seconds
        metrics, metrics_token = start_run_metrics()
        try:
            state = await self.graph.ainvoke(
                {"run_id": run_id, "request_ref": ctx.turn.request_ref},
                config={
                    "configurable": {"thread_id": checkpoint_id},
                    "recursion_limit": 64,
                },
                context=SurveyRuntime(
                    chains=self.chains,
                    artifacts=self.artifacts,
                    channels=self.channels,
                    emit=ctx.emit,
                    cancel=ctx.cancel,
                    deadline=deadline,
                    metrics=metrics,
                ),
            )
        except NodeCancelledError:
            if ctx.cancel.is_set():
                raise asyncio.CancelledError from None
            raise
        finally:
            reset_run_metrics(metrics_token)
        return AgentOutcome(
            result_ref=state["result_ref"],
            next_context_ref=ctx.thread.context_ref,
        )

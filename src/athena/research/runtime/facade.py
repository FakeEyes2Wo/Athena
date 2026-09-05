"""Composition root for Athena's autonomous research Supervisor."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.contracts import ArtifactRef
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.core.tool import ToolRegistry
from athena.core.tool_types import AskUser
from athena.execution.compute_config import ComputeConfig, load_compute_config
from athena.execution.runtime import CommandResult, ExecutionRuntime
from athena.kaggle import KaggleStack
from athena.research.config import ResearchConfig, SearchLimits, SurveyConfig
from athena.research.contracts import ValidationResult
from athena.research.evaluation import TrustedEvaluator
from athena.research.literature.paper_rag.models import PaperSummary
from athena.research.literature.survey import SurveyStack
from athena.research.prepare.authority import BaselineAuthorityStore
from athena.research.runtime.bootstrap import (
    baseline_ideator_tools as baseline_ideator_tools_impl,
)
from athena.research.runtime.bootstrap import (
    build_paths,
    build_services,
    wire_workflow,
)
from athena.research.runtime.bootstrap import (
    ideator_tools as ideator_tools_impl,
)
from athena.research.runtime.bootstrap import (
    kaggle_stack as kaggle_stack_impl,
)
from athena.research.runtime.bootstrap import (
    kaggle_tools as kaggle_tools_impl,
)
from athena.research.runtime.bootstrap import (
    plan_tools as plan_tools_impl,
)
from athena.research.runtime.bootstrap import (
    register_supervisor as register_supervisor_impl,
)
from athena.research.runtime.clarification import (
    confirm_and_start as confirm_and_start_impl,
)
from athena.research.runtime.clarification import (
    confirm_pending_task,
    recover_confirmation,
    seed_unconfirmed_task,
)
from athena.research.runtime.control import (
    message as message_impl,
)
from athena.research.runtime.control import (
    resume_current_task as resume_current_task_impl,
)
from athena.research.runtime.control import (
    start as start_impl,
)
from athena.research.runtime.control import (
    start_task as start_task_impl,
)
from athena.research.runtime.control import (
    start_validation as start_validation_impl,
)
from athena.research.runtime.corpus import (
    corpus_paper_ids as corpus_paper_ids_impl,
)
from athena.research.runtime.corpus import (
    corpus_papers_read as corpus_papers_read_impl,
)
from athena.research.runtime.corpus import (
    corpus_passages_read as corpus_passages_read_impl,
)
from athena.research.runtime.corpus import (
    corpus_summaries as corpus_summaries_impl,
)
from athena.research.runtime.corpus import (
    corpus_tools as corpus_tools_impl,
)
from athena.research.runtime.corpus import (
    start_corpus_round as start_corpus_round_impl,
)
from athena.research.runtime.events import RuntimeEvents
from athena.research.runtime.resume_contract import is_continue_command
from athena.research.runtime.settings import SettingsController
from athena.research.runtime.survey import (
    ensure_survey_stack as ensure_survey_stack_impl,
)
from athena.research.runtime.survey import (
    project_survey_event as project_survey_event_impl,
)
from athena.research.runtime.survey import (
    run_survey as run_survey_impl,
)
from athena.research.runtime.survey import (
    start_survey as start_survey_impl,
)
from athena.research.runtime.survey import (
    survey_topic as survey_topic_impl,
)
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.experiment import PlanTurnResult
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor

logger = logging.getLogger(__name__)

DEFAULT_SURVEY_PAPERS = 20

EmitFn = Callable[[str, dict[str, object]], Awaitable[None] | None]
PreparePhase = Callable[[], Awaitable[PrepareResult]]
ValidationPhase = Callable[[str, float], Awaitable[ValidationResult]]
PlanTurn = Callable[[str, ResearchState], Awaitable[PlanTurnResult]]


class ResearchRuntime:
    """Build infrastructure once and delegate all research mutation to Supervisor."""

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        state_root: str | Path | None = None,
        session_id: str = "default",
        model: str | None = None,
        client: Any = None,
        task: str = "",
        auto_seed_task: bool = False,
        search_limit: int | None = None,
        concurrency: int = 1,
        ideator_count: int = 3,
        hypotheses_per_ideator: int = 2,
        auto_validate: bool = False,
        skip_validate: bool = False,
        task_confirmation_gate: bool = False,
        auto_confirm: bool = False,
        direction: Literal["maximize", "minimize"] = "maximize",
        tolerance: float = 0.0,
        ideation: Literal["ideageneration", "baseline", "debate"] = "ideageneration",
        dataset_path: str | Path | None = None,
        target_column: str | None = None,
        split_seed: int = 0,
        group_column: str | None = None,
        data_root: str | Path | None = None,
        experiment_timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S,
        compute: ComputeConfig | None = None,
        # Kept for provider-less test adapters; production providers must use
        # the authoritative PREPARE orchestrator.
        prepare_phase: PreparePhase | None = None,
        validation_phase: ValidationPhase | None = None,
        plan_turn: Callable[[str, Any], Awaitable[PlanTurnResult]] | None = None,
        ask_user: AskUser | None = None,
        broker: Any | None = None,
        baseline_authority: BaselineAuthorityStore | None = None,
        survey: bool = False,
        survey_query: str = "",
        survey_max_papers: int = DEFAULT_SURVEY_PAPERS,
        survey_search_top_k: int = 0,
        survey_max_seconds: float = 0.0,
    ) -> None:
        paths = build_paths(project_root, state_root)
        search = SearchLimits(
            search_limit=10 if search_limit is None else search_limit,
            concurrency=concurrency,
            ideator_count=ideator_count,
            hypotheses_per_ideator=hypotheses_per_ideator,
        )
        survey_config = SurveyConfig(
            enabled=survey,
            query=survey_query,
            max_papers=survey_max_papers,
            search_top_k=survey_search_top_k,
            max_seconds=survey_max_seconds,
        )
        config = ResearchConfig(
            paths=paths,
            search=search,
            survey=survey_config,
            session_id=session_id,
            model=model,
            client=client,
            task=task,
            auto_seed_task=auto_seed_task,
            task_confirmation_gate=task_confirmation_gate,
            auto_confirm=auto_confirm,
            auto_validate=auto_validate,
            skip_validate=skip_validate,
            direction=direction,
            tolerance=tolerance,
            ideation=ideation,
            dataset_path=Path(dataset_path).resolve() if dataset_path else None,
            target_column=target_column,
            split_seed=split_seed,
            group_column=group_column,
            data_root=Path(data_root).resolve() if data_root else None,
            experiment_timeout_s=experiment_timeout_s,
            compute=compute if compute is not None else load_compute_config(),
            prepare_phase=prepare_phase,
            validation_phase=validation_phase,
            plan_turn=plan_turn,
            ask_user=ask_user,
        )

        provider = (
            ResponsesProvider(model, client=client) if model is not None else None
        )
        services, session = build_services(
            config,
            broker,
            baseline_authority,
            provider=provider,
        )
        self._config = config
        self._services = services
        self._session = session
        self._settings = SettingsController(self)
        wire_workflow(self)
        if provider is not None:
            self.register_supervisor(provider=provider)

    @property
    def state(self) -> ResearchState:
        """Return the Supervisor-owned durable state."""
        return self._services.durable.state

    @property
    def tree(self) -> ResearchTree:
        """Return the Supervisor-owned research history."""
        return self._services.durable.tree

    @property
    def supervisor(self) -> Supervisor:
        """Return the lifecycle coordinator."""
        supervisor = self._services.workflow.supervisor
        if supervisor is None:
            raise RuntimeError("Supervisor workflow is not wired")
        return supervisor

    @property
    def root(self) -> Path:
        """Return the research project root."""
        return self._config.paths.root

    @property
    def session_id(self) -> str:
        """Return the identity shared by runtime and human requests."""
        return self._config.session_id

    @property
    def workspaces_root(self) -> Path:
        """Return the directory that contains isolated plan workspaces."""
        return self._config.paths.workspaces

    @property
    def state_path(self) -> Path:
        """Return the durable lifecycle checkpoint path."""
        return self._config.paths.state

    @property
    def clarification_path(self) -> Path:
        """Return the canonical clarification draft path."""
        return self._config.paths.clarification

    @property
    def handoffs_path(self) -> Path:
        """Return the directory containing named task handoffs."""
        return self._config.paths.handoffs

    @property
    def store(self) -> LocalArtifactStore:
        """Return the content-addressed artifact store."""
        return self._services.infrastructure.store

    @property
    def events(self) -> RuntimeEvents:
        """Return the runtime event publisher and transcript store."""
        return self._services.infrastructure.events

    @property
    def registry(self) -> AgentTypeRegistry:
        """Return the registered agent type catalog."""
        return self._services.infrastructure.registry

    @property
    def agents(self) -> AgentRuntime:
        """Return the shared agent process runtime."""
        return self._services.infrastructure.agents

    @property
    def execution(self) -> ExecutionRuntime:
        """Return the default local execution runtime."""
        return self._services.infrastructure.execution

    async def execution_for(self, plan_id: str, workspace: Path) -> ExecutionRuntime:
        """给一个 Plan 拿到它该用的执行运行时（本地共享或远程租约）。"""
        pool = self._session.compute.pool
        if pool is None:
            return self.execution
        lease = self._session.compute.leases.get(plan_id)
        compute = self._session.compute.config
        if lease is None:
            lease = await pool.acquire(
                plan_id,
                local_workspace=Path(workspace),
                gpus=compute.gpus_per_experiment if compute else 1,
                timeout_s=compute.queue_timeout_s if compute else None,
            )
            self._session.compute.leases[plan_id] = lease
            logger.info(
                "plan %s leased %s gpu %s",
                plan_id,
                lease.card.name,
                list(lease.gpu_ids),
            )
        return ExecutionRuntime(
            project_root=self.root,
            environment_root=self.root,
            data_root=self._session.compute.data_root,
            store=self.store,
            backend=lease.backend,
        )

    def placement_for(self, plan_id: str) -> dict[str, Any] | None:
        """这个 Plan 跑在哪台机器、哪几张卡上；本地算力时为 None。"""
        lease = self._session.compute.leases.get(plan_id)
        return None if lease is None else lease.placement()

    async def release_lease(self, plan_id: str) -> None:
        """归还一个 Plan 的租约（关通道 → 远端清场 → 卡回池子）。"""
        pool = self._session.compute.pool
        if pool is None or plan_id not in self._session.compute.leases:
            return
        self._session.compute.leases.pop(plan_id, None)
        await pool.release(plan_id)

    @property
    def scripts(self) -> DataScriptRunner:
        """Return the trusted data-script runner."""
        return self._services.infrastructure.scripts

    @property
    def evaluator(self) -> TrustedEvaluator:
        """Return the trusted metric evaluator."""
        return self._services.infrastructure.evaluator

    @property
    def baseline_authority(self) -> BaselineAuthorityStore | None:
        """Return the controller-bound external baseline authority capability."""
        return self._services.infrastructure.baseline_authority

    @property
    def git(self) -> LocalGitWorkspace:
        """Return the workspace version-control service."""
        return self._services.infrastructure.git

    @property
    def provider(self) -> object | None:
        """Return the registered model provider."""
        return self._session.lifecycle.provider

    @property
    def task_text(self) -> str:
        """Return the effective original task text."""
        return self._session.lifecycle.task_text

    @property
    def model(self) -> str | None:
        """Return the configured model identifier."""
        return self._config.model

    @property
    def client(self) -> Any:
        """Return the optional provider client override."""
        return self._config.client

    @property
    def direction(self) -> Literal["maximize", "minimize"]:
        """Return the configured metric optimization direction."""
        return self._session.options.direction

    @property
    def ideation(self):
        """Return the configured hypothesis-generation strategy."""
        return self._session.options.ideation

    @property
    def prepare_phase(self):
        """Return the optional PREPARE phase override."""
        return self._config.prepare_phase

    @property
    def validation_phase(self):
        """Return the optional VALIDATE phase override."""
        return self._config.validation_phase

    @property
    def config(self):
        """Return the immutable runtime configuration."""
        return self._config

    @property
    def services(self):
        """Return the long-lived infrastructure container."""
        return self._services

    @property
    def session(self):
        """Return mutable state owned by this process."""
        return self._session

    @property
    def plan_turn(self):
        """Return the optional plan-turn override."""
        return self._config.plan_turn

    def register_supervisor(self, *, provider: object) -> None:
        """Register the long-lived SupervisorAgent once."""
        register_supervisor_impl(self, provider)

    def plan_tools(self) -> ToolRegistry | None:
        """写代码那一步的额外工具：Kaggle（若接入）+ 文献语料的只读检索算子。

        此前只有 Ideator 能查语料，于是文献只能影响"试什么"，永远影响不了"怎么实现"
        ——而后者才是论文真正给得出细节的地方：损失函数的写法、超参区间、预处理口径。
        一条假设写着"用 focal loss"，实现时该取什么 gamma、要不要配合重采样，答案就在
        那几篇论文里，而 PlanAgent 够不着。

        与 Ideator 用各自独立的会话：已读账本按 Agent 独立，而引用核验只看 ideation
        那一轮的账本，PlanAgent 读了什么不该算进去。
        """
        return plan_tools_impl(self)

    def kaggle_stack(self) -> KaggleStack:
        """Lazily build the shared Kaggle stack rooted at the project root.

        所有 agent（general / evaluator / prepare）共用同一份 stack：下载落在
        项目根下的 ``<slug>/`` 目录，EDA/SOTA 引用进共享 artifact 存储。这样
        PREPARE 下载的数据 SEARCH 也能经 ``shell_command`` 绝对路径读取。
        """
        return kaggle_stack_impl(self)

    def kaggle_tools(self, agent_type: str) -> ToolRegistry | None:
        """Return the minimal Kaggle tool set for ``agent_type``; None when off/unconfigured."""
        return kaggle_tools_impl(self, agent_type)

    def ideator_tools(self) -> Callable[[], ToolRegistry | None]:
        """Return a lazy provider for Ideator lane extra tools.

        Kaggle tools come from the shared stack when enabled; corpus operators are
        appended once the literature survey has built a corpus. The provider shape
        keeps registration time and per-lane tool assembly separate, so a corpus
        that finishes after PREPARE is picked up by the next Ideator lane.
        """

        return ideator_tools_impl(self)

    def baseline_ideator_tools(self) -> Callable[[], ToolRegistry]:
        """Return a lazy web-enabled provider for the baseline ideator only."""
        return baseline_ideator_tools_impl(self)

    def corpus_tools(self, *, for_ideation: bool = False) -> ToolRegistry | None:
        """Return read-only paper operators for the active corpus, if any."""
        return corpus_tools_impl(self, for_ideation=for_ideation)

    def start_corpus_round(self) -> None:
        """Drop the previous ideation round's read ledger."""
        start_corpus_round_impl(self)

    def corpus_papers_read(self) -> set[str]:
        """Paper ids actually opened during this ideation round."""
        return corpus_papers_read_impl(self)

    async def corpus_passages_read(self) -> dict[str, list[str]]:
        """Passages actually read this round, grouped by paper id."""
        return await corpus_passages_read_impl(self)

    async def corpus_summaries(self) -> list[PaperSummary]:
        """One-line summaries for every paper in the active corpus."""
        return await corpus_summaries_impl(self)

    async def corpus_paper_ids(self) -> set[str]:
        """Paper ids that actually exist in the active corpus."""
        return await corpus_paper_ids_impl(self)

    @property
    def tree_path(self) -> Path:
        """Return the on-disk research tree path."""
        return self._config.paths.tree

    def save_tree(self) -> Path:
        """Persist the current research tree to disk and return the path."""
        return self.tree.save(self.tree_path)

    def load_tree(self) -> ResearchTree:
        """Reload the research tree from disk (a fresh read-only snapshot)."""
        return ResearchTree.load(self.tree_path)

    def settings(self) -> dict[str, Any]:
        """Return a GUI-facing snapshot of runtime settings."""
        return self._settings.snapshot()

    async def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a whitelisted settings patch and persist durable fields."""
        return await self._settings.apply(patch)

    async def start(self) -> asyncio.Task[None]:
        """Start infrastructure and the single Supervisor loop once.

        Returns the Supervisor lifecycle task; callers may await it to block
        until PREPARE/SEARCH/VALIDATE reaches a terminal state.
        """
        await recover_confirmation(self)
        await confirm_pending_task(self)
        return await start_impl(self)

    def start_survey(self) -> None:
        """Start the background survey once, unless a corpus already exists."""
        start_survey_impl(self)

    def survey_corpus_ref(self) -> str | None:
        """Return the currently available corpus reference, if any."""
        return self.state.corpus_ref

    async def survey_topic(self) -> str:
        """Return the survey query: explicit setting first, task-derived otherwise."""
        return await survey_topic_impl(self)

    async def run_survey(self) -> None:
        """Run one full survey and record the resulting corpus reference."""
        await run_survey_impl(self)

    def ensure_survey_stack(self) -> SurveyStack:
        """Build and cache the survey dependency stack on this runtime's store."""
        return ensure_survey_stack_impl(self)

    async def project_survey_event(
        self, kind: str, _ref: str, data: dict[str, Any] | None = None
    ) -> None:
        """Project a survey progress event into a human-readable output line."""
        await project_survey_event_impl(self, kind, _ref, data)

    async def start_task(self, task: str) -> str:
        """Seed a task through PREPARE and SEARCH, with policy-based finalization.

        When ``skip_validate`` is enabled, SEARCH writes the Final report and
        enters ``COMPLETED`` directly; no durable FINAL phase is introduced.
        Otherwise the existing VALIDATE policy remains in effect.
        """
        if is_continue_command(task):
            return await self.resume_current_task()
        if self.state.task_understanding is None:
            await seed_unconfirmed_task(self, task)
        return await start_task_impl(self, task)

    async def resume_current_task(self) -> str:
        """Resume the current durable task without changing its confirmed contract."""
        return await resume_current_task_impl(self)

    def _clarification_controller_or_raise(self):
        controller = self._services.workflow.clarification
        if controller is None:
            raise ValueError("clarification controller is not configured")
        return controller

    async def task_clarification_start(self, task: str) -> object:
        """Start or resume clarification for the active session."""
        return await self._clarification_controller_or_raise().start_or_resume(task)

    async def task_clarification_get(self, draft_id: str) -> object:
        """Return the authoritative latest clarification draft."""
        return self._clarification_controller_or_raise().get(draft_id)

    async def task_clarification_retry(self, draft_id: str, revision: int) -> object:
        """Retry a retryable FAILED draft."""
        return self._clarification_controller_or_raise().retry(draft_id, revision)

    async def task_clarification_revise(
        self, draft_id: str, revision: int, instruction: str
    ) -> object:
        """Revise a ready draft and return it to CLARIFYING."""
        return self._clarification_controller_or_raise().revise(
            draft_id, revision, instruction
        )

    async def task_clarification_cancel(self, draft_id: str, revision: int) -> object:
        """Cancel the active clarification draft and return the session to IDLE."""
        return await self._clarification_controller_or_raise().cancel(
            draft_id, revision
        )

    async def confirm_and_start(
        self,
        draft_id: str,
        revision: int,
        acknowledge_unresolved: bool,
    ) -> object:
        """Confirm the latest clarification revision and start PREPARE atomically."""
        return await confirm_and_start_impl(
            self,
            draft_id,
            revision,
            acknowledge_unresolved,
        )

    async def start_validation(self) -> str:
        """Run the frozen-SOTA VALIDATE phase and publish the final report."""
        return await start_validation_impl(self)

    async def message(self, text: str) -> str:
        """Apply exact control commands or delegate ordinary prose unchanged."""
        return await message_impl(self, text)

    def subscribe(self, emit: EmitFn) -> str:
        """Subscribe and immediately receive one complete state snapshot."""
        return self.events.subscribe(emit)

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove one runtime event subscription."""
        self.events.unsubscribe(subscription_id)

    async def publish_output(
        self,
        *,
        source: Literal["supervisor", "agent", "tool"],
        channel: Literal["text", "stdout", "stderr", "error"],
        text: str,
        plan: str | None = None,
        tool: str | None = None,
        artifact_ref: ArtifactRef | None = None,
        persist: bool = True,
        message_id: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> None:
        """Publish one human-readable runtime output event."""
        await self.events.publish_output(
            source=source,
            channel=channel,
            text=text,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
            persist=persist,
            message_id=message_id,
            session_id=session_id,
            scope=scope,
            scope_id=scope_id,
        )

    async def project_command_result(
        self,
        result: CommandResult,
        *,
        plan: str | None = None,
        tool: str = "shell_command",
    ) -> None:
        """Project command output into the runtime event stream."""
        await self.events.project_command_result(result, plan=plan, tool=tool)

    def persist_user_message(self, text: str) -> None:
        """Append one human message to the durable transcript."""
        self.events.persist_user_message(text)

    def replay_output_events(self) -> list[dict[str, object]]:
        """Return output events recovered from the durable transcript."""
        return self.events.replay_output_events()

    async def suspend(self) -> str:
        """Park a live run at WAITING so a torn-down session stops reading as running.

        Deliberately *not* called from ``aclose()``: for the headless
        entrypoints a persisted ``RUNNING`` is the resume token that lets
        ``SearchLoop.run_search`` keep scheduling, so only the owner of a
        session's lifecycle (the GUI gateway) may downgrade it.
        """
        return await self.supervisor.suspend()

    async def aclose(self) -> None:
        """Release remote, event, survey, Supervisor, and agent resources."""
        # 先还租约：通道一关远端才杀进程组，漏掉会一直占着显存。
        if self._session.compute.pool is not None:
            await self._session.compute.pool.aclose()
            self._session.compute.leases.clear()
        await self.events.aclose()
        # 后台调研不属于任何 Plan，Supervisor.stop 管不到它；不在这里取消就会在
        # runtime 关掉之后继续下载、继续调模型，还会往已清空的订阅者发布。
        survey_task = self._session.survey.task
        if survey_task is not None and not survey_task.done():
            survey_task.cancel()
            await asyncio.gather(survey_task, return_exceptions=True)
        await self.supervisor.stop()
        task = self._session.lifecycle.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.agents.aclose()


__all__ = ["ResearchRuntime"]

"""Composition root for Athena's autonomous research Supervisor."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, Literal

from athena.agents.rubric_agent import register_rubric_agents
from athena.agents.supervisor_agent import register_supervisor_agent
from athena.agents.task_agents import register_plan_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.contracts import ArtifactRef
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.core.research_models import TaskUnderstanding
from athena.core.tool import ToolRegistry
from athena.core.tool_types import AskUser
from athena.execution.compute_config import ComputeConfig, load_compute_config
from athena.execution.pool import GpuPool
from athena.execution.runtime import CommandResult, ExecutionRuntime
from athena.kaggle import (
    AGENT_KAGGLE_TOOLS,
    KaggleStack,
    build_kaggle_stack,
    build_kaggle_tools,
)
from athena.research.agent_turn_runner import AgentTurnRunner
from athena.research.config import (
    ResearchConfig,
    ResearchPaths,
    SearchLimits,
    SurveyConfig,
)
from athena.research.contracts import ValidationResult
from athena.research.evaluation import TrustedEvaluator
from athena.research.paper_rag.schemas import PaperSummary
from athena.research.phase_runner import PhaseRunner
from athena.research.runtime_control import (
    cancel_supervisor_task as cancel_supervisor_task_impl,
    ensure_evaluation_policy as ensure_evaluation_policy_impl,
    ensure_started as ensure_started_impl,
    maybe_run_task_understanding as maybe_run_task_understanding_impl,
    message as message_impl,
    rearm_if_terminal as rearm_if_terminal_impl,
    recent_user_texts_from as recent_user_texts_from_impl,
    resume_task_text as resume_task_text_impl,
    start as start_impl,
    start_task as start_task_impl,
    start_validation as start_validation_impl,
    task_context_text as task_context_text_impl,
    task_needs_input as task_needs_input_impl,
    task_understanding as task_understanding_impl,
)
from athena.research.runtime_corpus import (
    corpus_paper_ids as corpus_paper_ids_impl,
    corpus_papers_read as corpus_papers_read_impl,
    corpus_passages_read as corpus_passages_read_impl,
    corpus_summaries as corpus_summaries_impl,
    corpus_tools as corpus_tools_impl,
    start_corpus_round as start_corpus_round_impl,
)
from athena.research.runtime_events import RuntimeEvents
from athena.research.runtime_settings import SettingsController
from athena.research.runtime_survey import (
    ensure_survey_stack as ensure_survey_stack_impl,
    project_survey_event as project_survey_event_impl,
    run_survey as run_survey_impl,
    start_survey as start_survey_impl,
    survey_topic as survey_topic_impl,
)
from athena.research.script_runner import DataScriptRunner
from athena.research.survey import SurveyStack
from athena.research.services import ResearchServices, ResearchSession
from athena.research.supervisor.events import EventProjector
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S
from athena.research.supervisor.experiment import PlanTurnResult
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor

logger = logging.getLogger(__name__)

DEFAULT_SURVEY_PAPERS = 20

EmitFn = Callable[[str, dict[str, object]], Awaitable[None] | None]
PreparePhase = Callable[[], Awaitable[PrepareResult]]
ValidationPhase = Callable[[str, float], Awaitable[ValidationResult]]
PlanTurn = Callable[[str, ResearchState], Awaitable[PlanTurnResult]]


def _merged(*registries: ToolRegistry | None) -> ToolRegistry | None:
    """合并若干可选工具表；全为空时返回 ``None``。

    ``ToolRegistry`` 不支持批量 merge，只能逐个 resolve/register。
    """
    present = [registry for registry in registries if registry is not None]
    if not present:
        return None
    merged = ToolRegistry()
    for registry in present:
        for spec in registry.specs:
            merged.register(registry.resolve(spec.name))
    return merged


class ResearchRuntime:
    """Build infrastructure once and delegate all research mutation to Supervisor."""

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        state_root: str | Path | None = None,
        model: str | None = None,
        reasoning_model: str | None = None,
        reasoning_thinking: bool = False,
        client: Any = None,
        task: str = "",
        auto_seed_task: bool = False,
        search_limit: int = 10,
        concurrency: int = 1,
        ideator_count: int = 3,
        hypotheses_per_ideator: int = 2,
        auto_validate: bool = False,
        direction: Literal["maximize", "minimize"] = "maximize",
        tolerance: float = 0.0,
        ideation: Literal["ideageneration", "baseline", "debate"] = "ideageneration",
        dataset_path: str | Path | None = None,
        target_column: str | None = None,
        split_seed: int = 0,
        data_root: str | Path | None = None,
        experiment_timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S,
        compute: ComputeConfig | None = None,
        prepare_phase: PreparePhase | None = None,
        validation_phase: ValidationPhase | None = None,
        plan_turn: Callable[[str, Any], Awaitable[PlanTurnResult]] | None = None,
        ask_user: AskUser | None = None,
        survey: bool = False,
        survey_query: str = "",
        survey_max_papers: int = DEFAULT_SURVEY_PAPERS,
        survey_search_top_k: int = 0,
        survey_max_seconds: float = 0.0,
    ) -> None:
        root = Path(project_root or ".").resolve()
        athena = (
            Path(state_root).resolve() if state_root is not None else root / ".athena"
        )
        workspaces = (
            root / "workspaces" if state_root is None else athena / "workspaces"
        )
        paths = ResearchPaths(
            root=root,
            athena=athena,
            workspaces=workspaces,
            state=athena / "state.json",
            tree=athena / "research_tree.json",
            sessions=athena / "logs" / "sessions",
        )
        config = ResearchConfig(
            paths=paths,
            model=model,
            client=client,
            task=task,
            auto_seed_task=auto_seed_task,
            search=SearchLimits(
                search_limit=search_limit,
                concurrency=concurrency,
                ideator_count=ideator_count,
                hypotheses_per_ideator=hypotheses_per_ideator,
            ),
            survey=SurveyConfig(
                enabled=survey,
                query=survey_query,
                max_papers=survey_max_papers,
                search_top_k=survey_search_top_k,
                max_seconds=survey_max_seconds,
            ),
            auto_validate=auto_validate,
            direction=direction,
            tolerance=tolerance,
            ideation=ideation,
            dataset_path=Path(dataset_path).resolve() if dataset_path else None,
            target_column=target_column,
            split_seed=split_seed,
            data_root=Path(data_root).resolve() if data_root else None,
            experiment_timeout_s=experiment_timeout_s,
            compute=compute if compute is not None else load_compute_config(),
            prepare_phase=prepare_phase,
            validation_phase=validation_phase,
            plan_turn=plan_turn,
            ask_user=ask_user,
        )

        store = LocalArtifactStore(paths.athena / "artifacts")
        registry = AgentTypeRegistry()
        agents = AgentRuntime(
            type_registry=registry,
            project_root=root,
            rollout_dir=paths.athena / "logs" / "agents",
        )
        execution = ExecutionRuntime(
            project_root=root,
            environment_root=root,
            data_root=config.data_root,
            store=store,
        )
        scripts = DataScriptRunner(
            store=store,
            workdir=paths.athena / "runs",
        )
        evaluator = TrustedEvaluator(scripts)
        git = LocalGitWorkspace(
            paths.athena / "repo",
            workspaces,
            store.put_bytes,
        )
        tree = ResearchTree.load(paths.tree) if paths.tree.is_file() else ResearchTree()
        state = (
            ResearchState.load(paths.state)
            if paths.state.is_file()
            else ResearchState(
                status="IDLE",
                phase="PREPARE",
                search_limit=search_limit,
                concurrency=concurrency,
                ideator_count=ideator_count,
                hypotheses_per_ideator=hypotheses_per_ideator,
            )
        )
        # 断点续传保护：跨目录拷贝来的 state 会携带旧项目的 eda_dir，使 PREPARE
        # 工作区/EDA 目录落到别的项目。强制校验其属于当前 project_root，否则置空
        # 让 PREPARE 按本项目重建——本项目只保留自身信息，唯一允许跨目录的是数据集源。
        # eda_dir 存的是相对项目根的路径（见 _run_prepare_phase），先解析成绝对再校验。
        eda_dir = state.eda_dir
        if eda_dir is not None:
            eda_path = Path(eda_dir)
            if not eda_path.is_absolute():
                eda_path = (root / eda_dir).resolve()
            if not eda_path.is_relative_to(root):
                state.eda_dir = None
        state.experiment_timeout_s = experiment_timeout_s
        if config.data_root is not None and state.data_root is None:
            state.data_root = str(config.data_root)

        events_projector = EventProjector(store)
        events_bus = RuntimeEvents(
            events=events_projector,
            store=store,
            sessions_dir=paths.sessions,
        )
        # 断点续传：恢复历史输出序列号，避免重启后新事件与重放历史 seq 冲突
        # 而被 TUI 去重丢弃。seq 全局单调，跨所有会话恢复到最大 seq。
        events_bus.resume_sequence()

        services = ResearchServices(
            store=store,
            registry=registry,
            agents=agents,
            execution=execution,
            git=git,
            events=events_bus,
            scripts=scripts,
            evaluator=evaluator,
            tree=tree,
            state=state,
        )
        session = ResearchSession(task_text=task)
        session.data_root = config.data_root
        session.compute = config.compute
        if session.compute is not None and session.compute.remote:
            session.pool = GpuPool(
                list(session.compute.hosts),
                placement=session.compute.placement,
                store=store,
                dataset_root=config.data_root,
            )
        self._config = config
        self._services = services
        self._session = session
        self._settings = SettingsController(self)

        agent_turns = AgentTurnRunner(self)
        phase_runner = PhaseRunner(self)
        supervisor = Supervisor(
            project_root=root,
            state_root=athena,
            state=state,
            tree=tree,
            store=store,
            agents=agents,
            workspaces=git,
            scheduler=Scheduler(),
            recovery=Recovery(),
            evaluator_ref=self._baseline_evaluator_ref(),
            run_plan_turn=phase_runner.run_plan_turn,
            run_supervisor_turn=agent_turns.run_supervisor_turn,
            run_ideator_turn=agent_turns.run_ideator_turn,
            run_hypothesis_rubric=agent_turns.run_hypothesis_rubric,
            run_general_turn=agent_turns.run_general_turn,
            publish=events_bus.publish_from_supervisor,
            auto_validate=auto_validate,
            direction=direction,
            tolerance=tolerance,
            run_prepare_phase=phase_runner.run_prepare_phase,
            run_validation_phase=phase_runner.run_validation_phase,
            publish_agent_event=events_bus.project_agent_event,
            on_plan_settled=self.release_lease,
        )
        if reasoning_thinking and reasoning_model is None:
            raise ValueError("reasoning_thinking requires reasoning_model")
        services.supervisor = supervisor
        services.agent_turns = agent_turns
        services.phase_runner = phase_runner
        events_bus.attach_supervisor(supervisor)
        if model is not None:
            provider = ResponsesProvider(model, client=client)
            reasoning_provider = (
                ResponsesProvider(
                    reasoning_model,
                    client=client,
                    thinking=reasoning_thinking,
                )
                if reasoning_model is not None
                else None
            )
            self.register_supervisor(
                provider=provider,
                reasoning_provider=reasoning_provider,
            )

    # ── Compatibility accessors: keep existing method bodies small while the
    # runtime now stores only _config/_services/_session. Tests that construct
    # ResearchRuntime.__new__ may still assign private names directly; those
    # assignments land in __dict__ and shadow the derived values below.

    _SESSION_FIELDS: ClassVar[dict[str, str]] = {
        "_provider": "provider",
        "_reasoning_provider": "reasoning_provider",
        "_task": "task",
        "_started": "started",
        "_task_text": "task_text",
        "_survey_stack": "survey_stack",
        "_survey_task": "survey_task",
        "_corpus_sessions": "corpus_sessions",
        "_kaggle_stack": "kaggle_stack",
    }

    _CONFIG_FIELDS: ClassVar[dict[str, str | tuple[str, str]]] = {
        "_model": "model",
        "_client": "client",
        "_direction": "direction",
        "_tolerance": "tolerance",
        "_auto_validate": "auto_validate",
        "_prepare_phase": "prepare_phase",
        "_validation_phase": "validation_phase",
        "_ask_user": "ask_user",
        "_ideation": "ideation",
        "_survey_enabled": ("survey", "enabled"),
        "_survey_query": ("survey", "query"),
        "_survey_max_papers": ("survey", "max_papers"),
        "_survey_search_top_k": ("survey", "search_top_k"),
        "_survey_max_seconds": ("survey", "max_seconds"),
    }

    _PATH_FIELDS: ClassVar[dict[str, str]] = {
        "_root": "root",
        "_athena": "athena",
        "_workspaces_root": "workspaces",
        "_state_path": "state",
        "_tree_path": "tree",
        "_sessions_dir": "sessions",
    }

    _SERVICE_FIELDS: ClassVar[dict[str, str]] = {
        "_store": "store",
        "_events_bus": "events",
        "_registry": "registry",
        "_agents": "agents",
        "_execution": "execution",
        "_scripts": "scripts",
        "_evaluator": "evaluator",
        "_git": "git",
        "_tree": "tree",
        "_state": "state",
        "_supervisor": "supervisor",
        "_agent_turns": "agent_turns",
        "_phase_runner": "phase_runner",
    }

    def __getattr__(self, name: str):
        if name in self._SESSION_FIELDS:
            if "_session" in self.__dict__:
                return getattr(self.__dict__["_session"], self._SESSION_FIELDS[name])
            raise AttributeError(name)
        if name in self._CONFIG_FIELDS:
            if "_config" in self.__dict__:
                field = self._CONFIG_FIELDS[name]
                if isinstance(field, tuple):
                    return getattr(
                        getattr(self.__dict__["_config"], field[0]), field[1]
                    )
                return getattr(self.__dict__["_config"], field)
            raise AttributeError(name)
        if name in self._PATH_FIELDS:
            if "_config" in self.__dict__:
                return getattr(self.__dict__["_config"].paths, self._PATH_FIELDS[name])
            raise AttributeError(name)
        if name in self._SERVICE_FIELDS:
            if "_services" in self.__dict__:
                value = getattr(self.__dict__["_services"], self._SERVICE_FIELDS[name])
                if name == "_supervisor" and value is None:
                    raise AttributeError(name)
                return value
            raise AttributeError(name)
        if name == "_settings":
            controller = SettingsController(self)
            self.__dict__["_settings"] = controller
            return controller
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    def __setattr__(self, name: str, value) -> None:
        if name in self._SESSION_FIELDS and "_session" in self.__dict__:
            setattr(self.__dict__["_session"], self._SESSION_FIELDS[name], value)
            return
        object.__setattr__(self, name, value)

    @property
    def state(self) -> ResearchState:
        """Return the Supervisor-owned durable state."""
        return self._supervisor.state

    @property
    def tree(self) -> ResearchTree:
        """Return the Supervisor-owned research history."""
        return self._supervisor.tree

    @property
    def supervisor(self) -> Supervisor:
        return self._supervisor

    @property
    def supervisor_provider(self) -> object | None:
        return self._provider

    @property
    def root(self) -> Path:
        return self._root

    @property
    def workspaces_root(self) -> Path:
        return self._workspaces_root

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def store(self) -> LocalArtifactStore:
        return self._store

    @property
    def events(self) -> RuntimeEvents:
        return self._events_bus

    @property
    def registry(self) -> AgentTypeRegistry:
        return self._registry

    @property
    def agents(self) -> AgentRuntime:
        return self._agents

    @property
    def execution(self) -> ExecutionRuntime:
        return self._execution

    async def execution_for(self, plan_id: str, workspace: Path) -> ExecutionRuntime:
        """给一个 Plan 拿到它该用的执行运行时（本地共享或远程租约）。"""
        pool = self._session.pool
        if pool is None:
            return self._execution
        lease = self._session.leases.get(plan_id)
        compute = self._session.compute
        if lease is None:
            if not pool.cards():
                await pool.preflight()
            lease = await pool.acquire(
                plan_id,
                local_workspace=Path(workspace),
                gpus=compute.gpus_per_experiment if compute else 1,
                timeout_s=compute.queue_timeout_s if compute else None,
            )
            self._session.leases[plan_id] = lease
            logger.info(
                "plan %s leased %s gpu %s",
                plan_id,
                lease.host.name,
                list(lease.gpu_ids),
            )
        return ExecutionRuntime(
            project_root=self._root,
            environment_root=self._root,
            data_root=self._session.data_root,
            store=self._store,
            backend=lease.backend,
        )

    def placement_for(self, plan_id: str) -> dict[str, Any] | None:
        """这个 Plan 跑在哪台机器、哪几张卡上；本地算力时为 None。"""
        lease = self._session.leases.get(plan_id)
        return None if lease is None else lease.placement()

    async def release_lease(self, plan_id: str) -> None:
        """归还一个 Plan 的租约（关通道 → 远端清场 → 卡回池子）。"""
        pool = self._session.pool
        if pool is None or plan_id not in self._session.leases:
            return
        self._session.leases.pop(plan_id, None)
        await pool.release(plan_id)

    @property
    def scripts(self) -> DataScriptRunner:
        return self._scripts

    @property
    def evaluator(self) -> TrustedEvaluator:
        return self._evaluator

    @property
    def git(self) -> LocalGitWorkspace:
        return self._git

    @property
    def provider(self) -> object | None:
        return self._provider

    @property
    def reasoning_provider(self) -> object | None:
        session = self.__dict__.get("_session")
        if session is not None:
            return session.reasoning_provider
        # Compatibility for lightweight tests and integrations that construct
        # the runtime with ``__new__`` and seed the legacy private field.
        return self.__dict__.get("_reasoning_provider")

    @property
    def task_text(self) -> str:
        return self._task_text

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def client(self) -> Any:
        return self._client

    @property
    def direction(self) -> Literal["maximize", "minimize"]:
        return self._direction

    @property
    def ideation(self):
        return self._ideation

    @property
    def prepare_phase(self):
        return self._prepare_phase

    @property
    def validation_phase(self):
        return self._validation_phase

    @property
    def config(self):
        return self._config

    @property
    def plan_turn(self):
        return self._config.plan_turn

    def register_supervisor(
        self,
        *,
        provider: object,
        reasoning_provider: object | None = None,
    ) -> None:
        """Register the long-lived SupervisorAgent once."""
        if self._provider is not None:
            raise ValueError("SupervisorAgent provider is already registered")
        self._provider = provider
        self._reasoning_provider = reasoning_provider or provider
        # 只读 stack 供 Supervisor 的 kaggle_get_competition 查主指标（不缓存，
        # 避免提前固化 download 标志）。
        supervisor_kaggle = build_kaggle_stack(
            download_root=self._root, artifacts=self._store, download=True
        )
        register_supervisor_agent(
            self._registry,
            provider=provider,
            artifacts=self._store,
            actions=self._supervisor,
            ask_user=(
                (lambda _t, _u: self._ask_user) if self._ask_user is not None else None
            ),
            kaggle_stack=supervisor_kaggle,
        )
        register_rubric_agents(
            self._registry,
            provider=self._reasoning_provider,
            repair_provider=provider,
            artifacts=self._store,
        )
        register_plan_agent(
            self._registry,
            provider=provider,
            artifacts=self._store,
            workspace_for=self._supervisor.workspace_path,
            execution=self._execution,
            # plan agent 在构造期即注册，早于 Supervisor 任务理解，故惰性求值。
            extra_tools=self.plan_tools,
        )

    def plan_tools(self) -> ToolRegistry | None:
        """写代码那一步的额外工具：Kaggle（若接入）+ 文献语料的只读检索算子。

        此前只有 Ideator 能查语料，于是文献只能影响"试什么"，永远影响不了"怎么实现"
        ——而后者才是论文真正给得出细节的地方：损失函数的写法、超参区间、预处理口径。
        一条假设写着"用 focal loss"，实现时该取什么 gamma、要不要配合重采样，答案就在
        那几篇论文里，而 PlanAgent 够不着。

        与 Ideator 用各自独立的会话：已读账本按 Agent 独立，而引用核验只看 ideation
        那一轮的账本，PlanAgent 读了什么不该算进去。
        """
        return _merged(self.kaggle_tools("plan"), self.corpus_tools())

    def kaggle_stack(self) -> KaggleStack:
        """Lazily build the shared Kaggle stack rooted at the project root.

        所有 agent（general / evaluator / prepare）共用同一份 stack：下载落在
        项目根下的 ``<slug>/`` 目录，EDA/SOTA 引用进共享 artifact 存储。这样
        PREPARE 下载的数据 SEARCH 也能经 ``shell_command`` 绝对路径读取。
        """
        if self._kaggle_stack is None:
            self._kaggle_stack = build_kaggle_stack(
                download_root=self._root,
                artifacts=self._store,
                download=self._supervisor.kaggle_download,
            )
        return self._kaggle_stack

    def kaggle_tools(self, agent_type: str) -> ToolRegistry | None:
        """Return the minimal Kaggle tool set for ``agent_type``; None when off/unconfigured."""
        names = AGENT_KAGGLE_TOOLS.get(agent_type)
        if not names or not self._supervisor.kaggle_enabled:
            return None
        stack = self.kaggle_stack()
        if not stack.client.configured:
            return None
        return build_kaggle_tools(stack, names=names)

    def ideator_tools(self) -> Callable[[], ToolRegistry | None]:
        """Return a lazy provider for Ideator lane extra tools.

        Kaggle tools come from the shared stack when enabled; corpus operators are
        appended once the literature survey has built a corpus. The provider shape
        keeps registration time and per-lane tool assembly separate, so a corpus
        that finishes after PREPARE is picked up by the next Ideator lane.
        """

        def build() -> ToolRegistry | None:
            return _merged(
                self.kaggle_tools("ideator"),
                self.corpus_tools(for_ideation=True),
            )

        return build

    # ── 文献语料：一次性构建，Ideator 只读 ──────────────────────────────

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

    # ── GUI 门面扩展：树持久化与运行设置（供 gui_gateway 只读/控制）────────

    @property
    def tree_path(self) -> Path:
        """Return the on-disk research tree path."""
        return self._tree_path

    def save_tree(self) -> Path:
        """Persist the current research tree to disk and return the path."""
        return self.tree.save(self._tree_path)

    def load_tree(self) -> ResearchTree:
        """Reload the research tree from disk (a fresh read-only snapshot)."""
        return ResearchTree.load(self._tree_path)

    def _compute_settings(self) -> dict[str, Any]:
        """Serialize the active compute configuration for the GUI."""
        return self._settings._compute_settings()

    def settings(self) -> dict[str, Any]:
        """Return a GUI-facing snapshot of runtime settings."""
        return self._settings.snapshot()

    async def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a whitelisted settings patch and persist durable fields."""
        return await self._settings.apply(patch)

    def _recent_user_texts(self, limit: int = 6) -> list[str]:
        """Return recent Human messages from the persisted session transcript."""
        return recent_user_texts_from_impl(self, limit)

    @staticmethod
    def _task_context_text(task_text: str, prior: list[str]) -> str:
        """Build the supervisor task-understanding prompt from task + history."""
        return task_context_text_impl(task_text, prior)

    def _resume_task_text(self, fallback: str) -> str:
        """Reconstruct the effective task text from persisted resume state."""
        return resume_task_text_impl(self, fallback)

    def _task_understanding(self) -> TaskUnderstanding | None:
        return task_understanding_impl(self)

    def _task_needs_input(self) -> bool:
        return task_needs_input_impl(self)

    async def _ensure_evaluation_policy(self) -> None:
        await ensure_evaluation_policy_impl(self)

    async def _maybe_run_task_understanding(self) -> bool:
        return await maybe_run_task_understanding_impl(self)

    async def start(self) -> asyncio.Task[None]:
        """Start infrastructure and the single Supervisor loop once.

        Returns the Supervisor lifecycle task; callers may await it to block
        until PREPARE/SEARCH/VALIDATE reaches a terminal state.
        """
        return await start_impl(self)

    def _start_survey(self) -> None:
        """Start the background survey once, unless a corpus already exists."""
        start_survey_impl(self)

    def survey_corpus_ref(self) -> str | None:
        """Return the currently available corpus reference, if any."""
        return self.state.corpus_ref

    async def _survey_topic(self) -> str:
        """Return the survey query: explicit setting first, task-derived otherwise."""
        return await survey_topic_impl(self)

    async def _run_survey(self) -> None:
        """Run one full survey and record the resulting corpus reference."""
        await run_survey_impl(self)

    def _ensure_survey_stack(self) -> SurveyStack:
        """Build and cache the survey dependency stack on this runtime's store."""
        return ensure_survey_stack_impl(self)

    async def _project_survey_event(
        self, kind: str, _ref: str, data: dict[str, Any] | None = None
    ) -> None:
        """Project a survey progress event into a human-readable output line."""
        await project_survey_event_impl(self, kind, _ref, data)

    def _rearm_if_terminal(self) -> None:
        """Clear the done supervisor task so a terminal run can be restarted."""
        rearm_if_terminal_impl(self)

    async def start_task(self, task: str) -> str:
        """Seed the research task and start PREPARE -> SEARCH -> VALIDATE."""
        return await start_task_impl(self, task)

    async def start_validation(self) -> str:
        """Run the frozen-SOTA VALIDATE phase and publish the final report."""
        return await start_validation_impl(self)

    async def message(self, text: str) -> str:
        """Apply exact control commands or delegate ordinary prose unchanged."""
        return await message_impl(self, text)

    async def _ensure_started(self) -> None:
        """Start the Supervisor loop once a trusted baseline exists."""
        await ensure_started_impl(self)

    async def _cancel_supervisor_task(self) -> None:
        """Cancel the current Supervisor lifecycle task (pause/stop interrupt)."""
        await cancel_supervisor_task_impl(self)

    # ── 事件/订阅/持久化（委托 RuntimeEvents）────────────────────────

    def subscribe(self, emit: EmitFn) -> str:
        """Subscribe and immediately receive one complete state snapshot."""
        return self._events_bus.subscribe(emit)

    def unsubscribe(self, subscription_id: str) -> None:
        self._events_bus.unsubscribe(subscription_id)

    async def publish_output(
        self,
        *,
        source: Literal["supervisor", "agent", "tool"],
        channel: Literal["text", "stdout", "stderr", "error"],
        text: str,
        plan: str | None = None,
        tool: str | None = None,
        artifact_ref: ArtifactRef | None = None,
    ) -> None:
        await self._events_bus.publish_output(
            source=source,
            channel=channel,
            text=text,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
        )

    async def project_command_result(
        self,
        result: CommandResult,
        *,
        plan: str | None = None,
        tool: str = "shell_command",
    ) -> None:
        await self._events_bus.project_command_result(result, plan=plan, tool=tool)

    def persist_user_message(self, text: str) -> None:
        self._events_bus.persist_user_message(text)

    def replay_output_events(self) -> list[dict[str, object]]:
        return self._events_bus.replay_output_events()

    def _baseline_evaluator_ref(self) -> ArtifactRef | None:
        baselines = self._tree.experiments(kind="baseline")
        return baselines[0].plan.run_config_ref if baselines else None

    async def aclose(self) -> None:
        # 先还租约：通道一关远端才杀进程组，漏掉会一直占着显存。
        session = getattr(self, "_session", None)
        if session is not None and session.pool is not None:
            await session.pool.aclose()
            session.leases.clear()
        await self._events_bus.aclose()
        # 后台调研不属于任何 Plan，Supervisor.stop 管不到它；不在这里取消就会在
        # runtime 关掉之后继续下载、继续调模型，还会往已清空的订阅者发布。
        if self._survey_task is not None and not self._survey_task.done():
            self._survey_task.cancel()
            await asyncio.gather(self._survey_task, return_exceptions=True)
        await self._supervisor.stop()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._agents.aclose()


__all__ = ["ResearchRuntime"]

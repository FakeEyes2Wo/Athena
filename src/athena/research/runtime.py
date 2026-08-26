"""Composition root for Athena's autonomous research Supervisor."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, Literal

from athena.agents.supervisor_agent import register_supervisor_agent
from athena.agents.task_agents import register_plan_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.contracts import ArtifactRef
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.core.tool import ToolRegistry
from athena.core.tool_types import AskUser
from athena.execution.compute_config import (
    ComputeConfig,
    load_compute_config,
    parse_compute_config,
)
from athena.execution.pool import GpuPool, Lease
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
from athena.research.paper_rag.search import (
    RetrievalSession,
    corpus_overview,
    corpus_paper_ids,
)
from athena.research.paper_rag.tool import MAX_OVERVIEW_PAPERS
from athena.research.phase_runner import PhaseRunner
from athena.research.runtime_events import RuntimeEvents, recent_user_texts
from athena.research.script_runner import DataScriptRunner
from athena.research.services import ResearchServices, ResearchSession
from athena.research.supervisor.events import EventProjector
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S
from athena.research.supervisor.experiment import PlanTurnResult
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from athena.research.survey import (
    SurveyRequest,
    SurveyStack,
    build_survey_stack,
    build_survey_tools,
    run_survey,
)
from athena.utils.single_turn_chat import single_turn_chat

logger = logging.getLogger(__name__)

# GUI settings_set 白名单字段。
# direction / tolerance / auto_validate 为构造期参数，改动后仅影响后续 plan。
MODEL_CONNECTION_ENV_VARS: dict[str, str] = {
    "provider": "LLM_PROVIDER",
    "base_url": "BASE_URL",
    "model_name": "MODEL_NAME",
    "llm_api_key": "LLM_API_KEY",
}
"""模型连接字段 → 环境变量名；provider 只允许 deepseek/openai/qwen。"""

ALLOWED_MODEL_PROVIDERS = ("deepseek", "openai", "qwen")

DEFAULT_SURVEY_PAPERS = 20
SURVEY_PLAN_LABEL = "survey"
# 由研究任务提炼文献检索式的提示。任务原文不能直接当检索式：它带着数据集路径、
# 目标列名这些只对本机有意义的行，而 PaperScout 会把整段原样交给相关性打分模型。
SURVEY_QUERY_PROMPT = (
    "Turn the following machine-learning research task into one English literature "
    "search topic for an academic paper search engine. Name the problem type, data "
    "modality and the methods worth surveying. Drop dataset paths, column names, "
    "file names and metric values. Answer with the topic sentence only, no preamble "
    "and no quotes."
)


def _mask_secret(value: str | None) -> str:
    """把密钥匿名化成 ``sk-********f8e3`` 形态，绝不回传明文。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 8}{value[-4:]}"


def _upsert_dotenv(path: Path, updates: dict[str, str]) -> None:
    """把 ``KEY=value`` 写入 ``.env``，保留注释与既有行；重复键覆盖。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    updated: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                updated.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in updated:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


SETTINGS_WHITELIST: frozenset[str] = frozenset(
    {
        "concurrency",
        "search_limit",
        "direction",
        "tolerance",
        "auto_validate",
        "manual_mode",
        "ideation",
        "ideator_count",
        "hypotheses_per_ideator",
        "handoff_sources",
        "model_connection",
        "data_root",
        "experiment_timeout_s",
        "compute",
    }
)

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
            Path(state_root).resolve()
            if state_root is not None
            else root / ".athena"
        )
        workspaces = root / "workspaces" if state_root is None else athena / "workspaces"
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
        tree = (
            ResearchTree.load(paths.tree)
            if paths.tree.is_file()
            else ResearchTree()
        )
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
        services.supervisor = supervisor
        services.agent_turns = agent_turns
        services.phase_runner = phase_runner
        events_bus.attach_supervisor(supervisor)
        if model is not None:
            self.register_supervisor(provider=ResponsesProvider(model, client=client))

    # ── Compatibility accessors: keep existing method bodies small while the
    # runtime now stores only _config/_services/_session. Tests that construct
    # ResearchRuntime.__new__ may still assign private names directly; those
    # assignments land in __dict__ and shadow the derived values below.

    _SESSION_FIELDS: ClassVar[dict[str, str]] = {
        "_provider": "provider",
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
                    return getattr(getattr(self.__dict__["_config"], field[0]), field[1])
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
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

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

    def register_supervisor(self, *, provider: object) -> None:
        """Register the long-lived SupervisorAgent once."""
        if self._provider is not None:
            raise ValueError("SupervisorAgent provider is already registered")
        self._provider = provider
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
        """语料的只读检索算子；没有语料时返回 ``None``。

        只给读的那一组：``paper_survey``/``paper_fetch``/``paper_markdown`` 会写出
        新语料，摆在 Agent 面前迟早会被按下去，而一次全链路是十几分钟起步。

        判据只有 ``corpus_ref``，不看本进程是否跑过调研。语料是内容寻址的、``corpus_ref``
        是持久化状态，因此续跑（或本轮命中缓存语料）时 ``_survey_stack`` 必然是 None，
        而那恰恰是最该拿到算子的场合——真实跑测里正是这条路径让 Ideator 收到"去调
        paper_corpus_overview"的提示却一个算子都没有，0 次检索、0 条 sources。

        ``for_ideation`` 决定这个会话的已读账本算不算进引用核验。**默认不算**：核验要
        回答的是"提这条假设时读过什么"，而 PlanAgent 在实现阶段读的论文与那个问题无关。
        默认设成不追踪，是为了让以后新增的消费方不会无声地放宽核验——放宽的后果是奖励
        贴标签，而那正是这条链路已经付过一次学费的地方。
        """
        if self.survey_corpus_ref() is None:
            return None
        session = RetrievalSession(self._ensure_survey_stack().corpus_cache)
        if for_ideation:
            # 每个 Ideator 实例一个会话（已读集合必须按 Agent 独立），但会话要留在运行时
            # 手里：引用核验需要"这一轮谁真的打开过哪几篇"的账本。
            self._corpus_sessions.append(session)
        return build_survey_tools(
            self._ensure_survey_stack(),
            include_survey=False,
            include_producers=False,
            session=session,
        )

    def start_corpus_round(self) -> None:
        """开始新一轮 ideation：丢掉上一轮的会话账本。

        不清的话，上一轮读过的论文会一直算作"本轮读过"，引用核验会越来越松。
        """
        self._corpus_sessions.clear()

    def corpus_papers_read(self) -> set[str]:
        """本轮 ideation 里被真正打开过正文的论文。"""
        opened: set[str] = set()
        for session in self._corpus_sessions:
            opened |= session.read_papers()
        return opened

    async def corpus_passages_read(self) -> dict[str, list[str]]:
        """本轮 ideation 逐篇读过的正文，按 ``paper_id`` 归组。

        支持性判定要看的是**读过的那几段**，而不是整篇论文：拿整篇去问"支不支持"，问的
        就变成了"这篇论文大体上相关吗"——而那正是贴标签式引用能通过的那个问题。
        """
        corpus_ref = self.survey_corpus_ref()
        if corpus_ref is None:
            return {}
        chunk_ids: set[str] = set()
        for session in self._corpus_sessions:
            chunk_ids |= session.read_chunk_ids()
        if not chunk_ids:
            return {}
        stack = self._ensure_survey_stack()
        corpus = await stack.corpus_cache.load(self._store, corpus_ref)
        passages: dict[str, list[str]] = {}
        for chunk_id in sorted(chunk_ids):
            position = corpus.positions.get(chunk_id)
            if position is None:
                continue
            entry = corpus.index.entries[position]
            passages.setdefault(entry.paper_id, []).append(entry.text)
        return passages

    async def corpus_summaries(self) -> list[PaperSummary]:
        """语料的逐篇门面，用于把"里面有什么"直接写进 Ideator 的 prompt。

        纯索引读取：不调模型、不访网络、不碰句向量，8 篇实测约 26 毫秒。
        """
        corpus_ref = self.survey_corpus_ref()
        if corpus_ref is None:
            return []
        stack = self._ensure_survey_stack()
        corpus = await stack.corpus_cache.load(self._store, corpus_ref)
        return corpus_overview(corpus, [], MAX_OVERVIEW_PAPERS).summaries

    async def corpus_paper_ids(self) -> set[str]:
        """语料里真实存在的 paper id；假设引用的合法取值就是这一组。

        与 ``corpus_tools`` 同一条惰性装配规则：只要有 ``corpus_ref`` 就能核验引用，
        否则续跑时校验会拿到空集合而静默放行任何编造的 paper id。
        """
        corpus_ref = self.survey_corpus_ref()
        if corpus_ref is None:
            return set()
        stack = self._ensure_survey_stack()
        corpus = await stack.corpus_cache.load(self._store, corpus_ref)
        return corpus_paper_ids(corpus)

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
        session = getattr(self, "_session", None)
        compute = session.compute if session is not None else None
        if compute is None:
            return {
                "mode": "local",
                "placement": "pack",
                "fallback": "never",
                "gpus_per_experiment": 1,
                "queue_timeout_s": None,
                "hosts": [],
            }
        return {
            "mode": compute.mode,
            "placement": compute.placement,
            "fallback": compute.fallback,
            "gpus_per_experiment": compute.gpus_per_experiment,
            "queue_timeout_s": compute.queue_timeout_s,
            "hosts": [
                {
                    "name": host.name,
                    "ssh": host.alias,
                    "gpus": list(host.gpus) if host.gpus is not None else "auto",
                    "max_leases": host.max_leases,
                }
                for host in compute.hosts
            ],
        }

    def settings(self) -> dict[str, Any]:
        """Return a GUI-facing snapshot of runtime settings."""
        return {
            "project_root": str(self._root),
            "model": self._model,
            "concurrency": self.state.concurrency,
            "search_limit": self.state.search_limit,
            "ideation": self._ideation,
            "ideator_count": self.state.ideator_count,
            "hypotheses_per_ideator": self.state.hypotheses_per_ideator,
            "handoff_sources": self.state.handoff_sources,
            "direction": self._direction,
            "tolerance": self._tolerance,
            "auto_validate": self._auto_validate,
            "manual_mode": self.state.manual_mode,
            "phase": self.state.phase,
            "status": self.state.status,
            "data_root": self.state.data_root,
            "experiment_timeout_s": self.state.experiment_timeout_s,
            "compute": self._compute_settings(),
            "model_connection": {
                "provider": os.environ.get("LLM_PROVIDER") or "deepseek",
                "base_url": os.environ.get("BASE_URL") or "",
                "model_name": os.environ.get("MODEL_NAME") or "",
                "llm_api_key": _mask_secret(
                    os.environ.get("LLM_API_KEY")
                    or os.environ.get("DEEPSEEK_API_KEY")
                    or os.environ.get("OPENAI_API_KEY")
                ),
            },
        }

    async def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a whitelisted settings patch and persist durable fields.

        ``direction``/``tolerance``/``auto_validate`` only affect future plans
        (they are construction-time knobs), which the GUI labels as delayed.
        """
        allowed = SETTINGS_WHITELIST
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"unsupported settings fields: {sorted(unknown)}")
        if "concurrency" in patch:
            concurrency = patch["concurrency"]
            if not isinstance(concurrency, int) or concurrency < 1:
                raise ValueError("concurrency must be an integer >= 1")
            self.state.concurrency = concurrency
        if "search_limit" in patch:
            search_limit = patch["search_limit"]
            if not isinstance(search_limit, int) or search_limit < 0:
                raise ValueError("search_limit must be an integer >= 0")
            self.state.search_limit = search_limit
        if "ideation" in patch:
            ideation = patch["ideation"]
            if ideation not in {"ideageneration", "baseline", "debate"}:
                raise ValueError(
                    "ideation must be 'ideageneration', 'baseline', or 'debate'"
                )
            if ideation != self._ideation:
                self._ideation = ideation
                # 输出契约/prompt 在 Ideator 注册时绑定；切换模式后撤销旧注册，
                # 下一条 lane 会按新机制重新注册（debate 模式不走注册表）。
                self._registry.unregister("ideator")
        if "ideator_count" in patch:
            value = patch["ideator_count"]
            if not isinstance(value, int) or value < 1 or value > 8:
                raise ValueError("ideator_count must be an integer between 1 and 8")
            self.state.ideator_count = value
        if "hypotheses_per_ideator" in patch:
            value = patch["hypotheses_per_ideator"]
            if not isinstance(value, int) or value < 1 or value > 5:
                raise ValueError(
                    "hypotheses_per_ideator must be an integer between 1 and 5"
                )
            self.state.hypotheses_per_ideator = value
        if "handoff_sources" in patch:
            value = patch["handoff_sources"]
            if not isinstance(value, list) or any(
                source not in {"kaggle", "literature"} for source in value
            ):
                raise ValueError(
                    "handoff_sources must be a list containing only "
                    "'kaggle' and 'literature'"
                )
            self.state.handoff_sources = value
        if "manual_mode" in patch:
            manual = patch["manual_mode"]
            if not isinstance(manual, bool):
                raise ValueError("manual_mode must be a bool")
            if manual != self.state.manual_mode:
                await self.message("/manual" if manual else "/auto")
        if "direction" in patch:
            direction = patch["direction"]
            if direction not in {"maximize", "minimize"}:
                raise ValueError("direction must be 'maximize' or 'minimize'")
            self._direction = direction
        if "tolerance" in patch:
            tolerance = patch["tolerance"]
            if not isinstance(tolerance, (int, float)) or tolerance < 0:
                raise ValueError("tolerance must be a number >= 0")
            self._tolerance = float(tolerance)
        if "auto_validate" in patch:
            auto_validate = patch["auto_validate"]
            if not isinstance(auto_validate, bool):
                raise ValueError("auto_validate must be a bool")
            self._auto_validate = auto_validate
        if "experiment_timeout_s" in patch:
            value = patch["experiment_timeout_s"]
            if not isinstance(value, int) or value < 1:
                raise ValueError("experiment_timeout_s must be an integer >= 1")
            self.state.experiment_timeout_s = value
        if "data_root" in patch:
            raw = patch["data_root"]
            if raw in (None, ""):
                self._session.data_root = None
                self.state.data_root = None
            else:
                path = Path(str(raw)).resolve()
                if not path.is_dir():
                    raise ValueError(f"data_root does not exist: {path}")
                self._session.data_root = path
                self.state.data_root = str(path)
        if "compute" in patch:
            raw = patch["compute"]
            if not isinstance(raw, dict):
                raise ValueError("compute must be an object")
            new_compute = parse_compute_config(raw)
            self._session.compute = new_compute
            if new_compute.remote:
                if self._session.pool is None:
                    self._session.pool = GpuPool(
                        list(new_compute.hosts),
                        placement=new_compute.placement,
                        store=self._store,
                        dataset_root=self._session.data_root,
                    )
            elif self._session.pool is not None:
                await self._session.pool.aclose()
                self._session.pool = None
                self._session.leases.clear()
        if "model_connection" in patch:
            raw = patch["model_connection"]
            if not isinstance(raw, dict):
                raise ValueError("model_connection must be an object")
            updates: dict[str, str] = {}
            for field, env_key in MODEL_CONNECTION_ENV_VARS.items():
                value = raw.get(field)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise ValueError(f"{field} must be a string")
                value = value.strip()
                if field == "provider":
                    if value and value not in ALLOWED_MODEL_PROVIDERS:
                        raise ValueError(
                            f"provider must be one of {', '.join(ALLOWED_MODEL_PROVIDERS)}"
                        )
                if value:
                    updates[env_key] = value
            if updates:
                _upsert_dotenv(Path(".env"), updates)
                os.environ.update(updates)
        if any(
            field in patch
            for field in (
                "concurrency",
                "search_limit",
                "ideator_count",
                "hypotheses_per_ideator",
                "handoff_sources",
                "experiment_timeout_s",
                "data_root",
            )
        ):
            self.state.save(self._state_path)
        return self.settings()

    def _recent_user_texts(self, limit: int = 6) -> list[str]:
        """Return recent Human messages from the persisted session transcript."""
        try:
            records = self.replay_output_events()
        except Exception:
            logger.warning("failed to replay session transcript", exc_info=True)
            return []
        return recent_user_texts(records, limit)

    @staticmethod
    def _task_context_text(task_text: str, prior: list[str]) -> str:
        """Build the supervisor task-understanding prompt from task + history."""
        if not prior:
            return task_text
        return (
            "Previous conversation:\n"
            + "\n".join(f"- {text}" for text in prior)
            + "\n\nCurrent task text:\n"
            + task_text
        )

    def _resume_task_text(self, fallback: str) -> str:
        """Reconstruct the effective task text from persisted resume state.

        优先用首次持久化的完整任务文本；旧项目没有该字段时，用结构化任务
        理解（title/dataset/target）拼出描述，避免把 "continue" 喂给
        survey/PREPARE 提示词。
        """
        if self.state.task_text:
            return self.state.task_text
        understanding = self.state.task_understanding or {}
        parts = [
            str(understanding[key])
            for key in ("title", "dataset", "target")
            if understanding.get(key)
        ]
        return " ".join(parts) if parts else fallback

    async def _maybe_run_task_understanding(self) -> None:
        """PREPARE 阶段运行任务理解；已有断点则直接跳过。

        失败只降级为默认关闭 Kaggle 工具，不阻断启动。
        """
        if self._provider is None or self.state.phase != "PREPARE":
            return
        if self.state.task_understanding is not None:
            await self.publish_output(
                source="supervisor",
                channel="text",
                text="断点续传：复用已持久化的任务理解，跳过任务理解回合。",
            )
            return
        if not self._task_text.strip():
            return
        context = self._task_context_text(self._task_text, self._recent_user_texts())
        await self.publish_output(
            source="supervisor",
            channel="text",
            text="任务理解中：阅读任务并决定是否接入 Kaggle 工具…",
        )
        try:
            await self._agent_turns.run_supervisor_turn(context)
            await self.publish_output(
                source="supervisor", channel="text", text="任务理解完成。"
            )
        except Exception as error:
            logger.warning(
                "supervisor task-understanding turn failed; Kaggle tools stay off",
                exc_info=True,
            )
            await self.publish_output(
                source="supervisor",
                channel="error",
                text=f"任务理解失败（已降级继续）：{error}",
            )

    async def start(self) -> asyncio.Task[None]:
        """Start infrastructure and the single Supervisor loop once.

        Returns the Supervisor lifecycle task; callers may await it to block
        until PREPARE/SEARCH/VALIDATE reaches a terminal state.
        """
        if self._task is not None and not self._task.done():
            return self._task
        await self._git.init(initial_file=".gitignore", initial_content=".venv/\n")
        self._agents.start()
        # 与 PREPARE 并行起跑：调研要十几分钟，而 PREPARE 也不快，串起来等于白等一遍。
        self._start_survey()
        # 断点续传：直接 start()（而非 start_task）的重启路径也恢复首次任务文本。
        self._task_text = self._resume_task_text(self._task_text)
        if self.state.status == "IDLE":
            self.state.status = "RUNNING"
            self.state.save(self._state_path)
        self._started = True

        async def _run_lifecycle() -> None:
            # 任务理解也放进可取消的后台任务：否则 start_search RPC 会一直占住
            # 网关，pause/stop 根本进不来，且 self._task 还不存在、无法取消。
            await self._maybe_run_task_understanding()
            await self._supervisor.start()

        self._task = asyncio.create_task(_run_lifecycle())
        return self._task

    def _start_survey(self) -> None:
        """把文献调研作为后台任务起掉，与 PREPARE 并行。

        放在这里而不是 SEARCH 里，是因为检索式只取决于研究任务本身，不取决于
        PREPARE 造出什么样的 baseline——等 handoff 没有额外信息，却要白等一个
        多分钟的阶段。已经有语料（断点续传）时不重复付费。
        """
        if not self._survey_enabled or self._survey_task is not None:
            return
        if self.state.corpus_ref is not None:
            return
        self._survey_task = asyncio.create_task(self._run_survey())

    def survey_corpus_ref(self) -> str | None:
        """当前可用的论文语料引用；后台调研尚未完成时返回 ``None``。

        刻意不 await：SEARCH 绝不为调研停等。第一轮 ideation 若赶在语料建好之前，
        就照常只看数据集，从下一轮起自动带上文献。

        读的是 Supervisor 持有的那份 state：``recover()`` 会用 ``model_copy`` 换掉
        状态对象，写在旧对象上的字段会被单写者的下一次保存覆盖掉。
        """
        return self.state.corpus_ref

    async def _survey_topic(self) -> str:
        """本次调研的检索式：显式参数优先，否则由研究任务提炼一句主题。

        提炼失败时退回任务原文而不是抛错：任务原文带着数据集路径与列名，是个糟糕的
        检索式，但降级检索仍然好过没有检索——而抛错会让整段调研在还没发出一次请求
        之前就结束。
        """
        if self._survey_query.strip():
            return self._survey_query.strip()
        task = self._task_text.strip()
        if not task or self._model is None:
            return task
        try:
            topic = await single_turn_chat(
                task,
                model=self._model,
                client=self._client,
                system_prompt=SURVEY_QUERY_PROMPT,
            )
        except Exception:
            logger.warning(
                "survey topic rewrite failed; using the raw task", exc_info=True
            )
            return task
        return topic.strip() or task

    async def _run_survey(self) -> None:
        """跑一次全链路调研并把语料引用写进持久状态。

        任何失败都只发布成一条错误输出：文献是 ideation 的增益而不是前提，
        让它中断 PREPARE/SEARCH 会把一个可选能力变成单点故障。
        """
        try:
            topic = await self._survey_topic()
            stack = self._ensure_survey_stack()
            await self.publish_output(
                source="tool",
                channel="text",
                text=f"literature survey started: {topic}",
                plan=SURVEY_PLAN_LABEL,
                tool="paper_survey",
            )
            report = await run_survey(
                stack,
                SurveyRequest(query=topic, max_papers=self._survey_max_papers),
                emit=self._project_survey_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.publish_output(
                source="tool",
                channel="error",
                text=f"literature survey failed: {exc}",
                plan=SURVEY_PLAN_LABEL,
                tool="paper_survey",
            )
            return
        if report.corpus_ref is None:
            await self.publish_output(
                source="tool",
                channel="error",
                text=(
                    f"literature survey built no corpus (status={report.status}); "
                    "ideation continues on the dataset alone"
                ),
                plan=SURVEY_PLAN_LABEL,
                tool="paper_survey",
            )
            return
        self.state.corpus_ref = report.corpus_ref
        self.state.save(self._state_path)
        await self.publish_output(
            source="tool",
            channel="text",
            text=(
                f"literature corpus ready: {report.converted()} papers "
                f"({report.timings.total_seconds:.0f}s)"
            ),
            plan=SURVEY_PLAN_LABEL,
            tool="paper_survey",
        )

    def _ensure_survey_stack(self) -> SurveyStack:
        """装配（并缓存）文献链路依赖，复用本项目的 artifact 存储。

        共用一份 store 是硬要求：``corpus_ref`` 要和 ``state.json`` 里其他引用
        一起被本项目解析，落在两个 store 里就会出现"状态里记着、宿主取不到"。
        """
        if self._survey_stack is None:
            self._survey_stack = build_survey_stack(
                artifacts=self._store, client=self._client
            )
        return self._survey_stack

    async def _project_survey_event(
        self, kind: str, _ref: str, data: dict[str, Any] | None = None
    ) -> None:
        """把调研进度投影成一条可读输出。

        全链路十几分钟起步，其中七成花在检索上。不报进度的话，它在界面上与卡死
        没有区别——而它跑在后台，用户连"哪一步慢"都无从判断。
        """
        payload = data or {}
        if kind == "paper_scout/step":
            text = (
                f"scout step {payload.get('step', '?')}: "
                f"pool {payload.get('pool', 0)}"
            )
        elif kind == "survey/scouted":
            text = f"retrieved {payload.get('retained', 0)} of {payload.get('pool', 0)}"
        elif kind == "survey/fetched":
            text = (
                f"fetched {payload.get('fetched', 0)} of "
                f"{payload.get('attempted', 0)} attempted"
            )
        elif kind == "survey/converted":
            text = f"converted {payload.get('converted', 0)} papers"
        elif kind == "survey/indexed":
            text = f"indexed {payload.get('indexed', 0)} papers"
        else:
            return
        await self.publish_output(
            source="tool",
            channel="text",
            text=text,
            plan=SURVEY_PLAN_LABEL,
            tool="paper_survey",
        )

    def _rearm_if_terminal(self) -> None:
        """Clear the done supervisor task so a terminal run can be restarted."""
        if self._started and self._task is not None and self._task.done():
            if self.state.status in {"FAILED", "STOPPED", "COMPLETED"}:
                self._task = None

    async def start_task(self, task: str) -> str:
        """Seed the research task and start PREPARE -> SEARCH -> VALIDATE.

        Fresh runs begin at PREPARE so a trusted baseline/SOTA is established
        before any SEARCH hypothesis can be proposed. Existing ``state.json``
        (resume) keeps its phase and starts via ``recover()``. A terminal run
        is re-armed in place; ``eda_dir`` and workspaces are left untouched.
        断点续传时沿用首次持久化的完整任务文本，短消息（continue/retry）不会
        污染 survey 选题与 PREPARE 提示词；任务理解崩溃重试时也不覆盖已存任务。
        """
        if self.state.task_text is None and self.state.task_understanding is None:
            self.state.task_text = task
            self.state.save(self._state_path)
        self._task_text = self._resume_task_text(task)
        self._rearm_if_terminal()
        if not self._started or self._task is None:
            if self.tree.best_experiment_id() is None and self.state.phase != "PREPARE":
                self.state.phase = "PREPARE"
            await self.start()
        return self.state.status

    async def start_validation(self) -> str:
        """Run the frozen-SOTA VALIDATE phase and publish the final report.

        Wires the GUI ``start_validation`` control to the supervisor phase
        machine. In the interactive path (``auto_validate=False``) SEARCH parks
        at ``WAITING`` after its budget; this transitions into VALIDATE and
        runs it. Idempotent once validation has already completed.
        """
        if self.state.phase == "COMPLETED":
            return self.state.status
        await self._ensure_started()
        phase = self.state.phase
        if phase == "SEARCH":
            await self._supervisor.set_phase_decision("VALIDATE")
        elif phase == "VALIDATE":
            await self._supervisor.continue_phase()
        else:
            raise ValueError(f"cannot VALIDATE from phase {phase}; run SEARCH first")
        return self.state.status

    async def message(self, text: str) -> str:
        """Apply exact control commands or delegate ordinary prose unchanged.

        With ``auto_seed_task`` (TUI entry), the first ordinary message before
        ``start()`` seeds the research task and starts PREPARE, so a trusted
        baseline/SOTA exists before SEARCH proposes hypotheses.
        """
        command = text.strip()
        if command == "/stop":
            status = await self._supervisor.request_stop()
            await self._cancel_supervisor_task()
            return status
        if command == "/pause":
            status = await self._supervisor.pause()
            # PREPARE/VALIDATE 没有 SEARCH 那样的调度循环检查点，直接取消阶段任务，
            # 由 /resume 重新进入阶段机。
            if self.state.phase in {"PREPARE", "VALIDATE"}:
                await self._cancel_supervisor_task()
            return status
        if command == "/resume":
            if self._supervisor.is_stopped():
                return self.state.status
            if (self._task is None or self._task.done()) and self._started:
                await self._supervisor.resume(restarting=True)
                await self.start()
            else:
                await self._ensure_started()
                await self._supervisor.resume()
            return self.state.status
        if command == "/manual":
            await self._ensure_started()
            await self._supervisor.set_manual_mode(True)
            return "manual mode on"
        if command == "/auto":
            await self._ensure_started()
            await self._supervisor.set_manual_mode(False)
            return "manual mode off"
        if command.startswith("/select "):
            await self._ensure_started()
            hypothesis_id = command[len("/select ") :].strip()
            if not hypothesis_id:
                return "usage: /select <hypothesis_id>"
            await self._supervisor.select_next_hypothesis(hypothesis_id)
            return f"selected {hypothesis_id}"
        if self._auto_seed_task and not self._started:
            return await self.start_task(command)
        answer = await self._supervisor.message(text)
        # Interactive resume: a SupervisorAgent turn may transition an idle run
        # into VALIDATE. Re-enter the phase machine to actually execute it; the
        # live start() task (or auto_validate) handles the running case.
        if self.state.phase == "VALIDATE" and (self._task is None or self._task.done()):
            await self._supervisor.continue_phase()
        return answer

    async def _ensure_started(self) -> None:
        """Start (and recover) the Supervisor loop once a trusted baseline exists.

        No-op for a fresh project (no SOTA yet) so control commands never jump
        straight into SEARCH without PREPARE.
        """
        if not self._started and self.tree.best_experiment_id() is not None:
            await self.start()

    async def _cancel_supervisor_task(self) -> None:
        """Cancel the current Supervisor lifecycle task (pause/stop interrupt)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

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
        if self._session.pool is not None:
            await self._session.pool.aclose()
            self._session.leases.clear()
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

"""Build and connect the concrete services behind ``ResearchRuntime``."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from athena.agents.supervisor_agent import register_supervisor_agent
from athena.agents.task_agents import register_plan_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import BaseProvider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.core.tool import ToolRegistry
from athena.execution.pool import GpuPool
from athena.execution.runtime import ExecutionRuntime
from athena.kaggle import (
    AGENT_KAGGLE_TOOLS,
    KaggleStack,
    build_kaggle_stack,
    build_kaggle_tools,
)
from athena.research.clarification.controller import ClarificationController
from athena.research.clarification.generator import (
    DeterministicClarificationGenerator,
    ProgressSinkStage,
    ProgressSource,
)
from athena.research.clarification.llm_generator import (
    REPORT_TASK_UNDERSTANDING_TOOL,
    TASK_UNDERSTANDING_SCOPE,
    LLMClarificationGenerator,
)
from athena.research.clarification.persistence import ClarificationStore
from athena.research.config import (
    ResearchConfig,
    ResearchPaths,
    SearchLimits,
    SurveyConfig,
)
from athena.research.evaluation import TrustedEvaluator
from athena.research.prepare.authority import BaselineAuthorityStore
from athena.research.runtime.events import RuntimeEvents
from athena.research.runtime.phase_runner import PhaseRunner
from athena.research.runtime.services import (
    ComputeSession,
    DurableResearch,
    LifecycleSession,
    ResearchInfrastructure,
    ResearchServices,
    ResearchSession,
    RuntimeOptions,
)
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.deps import (
    PhaseActions,
    ResearchActions,
    SearchServices,
    SupervisorDeps,
    SupervisorPaths,
    SupervisorRuntime,
)
from athena.research.supervisor.events import EventProjector
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduling import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from athena.research.turns.runner import AgentTurnRunner
from athena.retrieval.web_search import build_web_tools


def build_paths(
    project_root: str | Path | None,
    state_root: str | Path | None,
) -> ResearchPaths:
    """Resolve durable and workspace paths for one runtime."""
    root = Path(project_root or ".").resolve()
    athena = Path(state_root).resolve() if state_root is not None else root / ".athena"
    # Named GUI sessions keep trusted state below ``.athena/conversations`` but
    # agent file tools deliberately reject writes anywhere below ``.athena``.
    # Namespace their Git worktrees in the public workspace tree instead of
    # weakening that protection.  ``athena.name`` is the validated session id
    # for GUI-created state roots.
    workspaces = root / "workspaces"
    if state_root is not None:
        workspaces = workspaces / "conversations" / athena.name
    return ResearchPaths(
        root=root,
        athena=athena,
        workspaces=workspaces,
        state=athena / "state.json",
        tree=athena / "research_tree.json",
        sessions=athena / "logs" / "sessions",
        clarification=athena / "clarification.json",
        clarification_confirmation=athena / "clarification-confirmation.json",
        handoffs=athena / "handoffs",
    )


def build_config(
    paths: ResearchPaths,
    search: SearchLimits,
    survey: SurveyConfig,
    fields: dict[str, Any],
) -> ResearchConfig:
    """Assemble immutable runtime configuration from grouped options."""
    return ResearchConfig(paths=paths, search=search, survey=survey, **fields)


def build_services(
    config: ResearchConfig,
    broker: object | None,
    baseline_authority: BaselineAuthorityStore | None = None,
    *,
    provider: BaseProvider | None = None,
) -> tuple[ResearchServices, ResearchSession]:
    """Build durable infrastructure and transient process state."""
    if config.model is not None and provider is None:
        raise ValueError("a configured model requires an explicit provider")

    paths = config.paths

    # Build process-wide infrastructure from the immutable configuration.
    store = LocalArtifactStore(paths.athena / "artifacts")
    registry = AgentTypeRegistry()
    agents = AgentRuntime(
        type_registry=registry,
        project_root=paths.root,
        rollout_dir=paths.athena / "logs" / "agents",
        session_id=config.session_id,
    )
    execution = ExecutionRuntime(
        project_root=paths.root,
        environment_root=paths.root,
        data_root=config.data_root,
        store=store,
    )
    scripts = DataScriptRunner(store=store, workdir=paths.athena / "runs")
    evaluator = TrustedEvaluator(scripts)
    git = LocalGitWorkspace(paths.athena / "repo", paths.workspaces, store.put_bytes)
    tree = ResearchTree.load(paths.tree) if paths.tree.is_file() else ResearchTree()
    state = _load_state(config)

    # Resume event sequencing before anything can publish a new event.
    events = RuntimeEvents(
        events=EventProjector(store),
        store=store,
        sessions_dir=paths.sessions,
    )
    events.resume_sequence()

    async def publish_clarification_progress(
        *,
        summary: str,
        stage: ProgressSinkStage,
        session_id: str,
        scope_id: str,
        source: ProgressSource,
        persist: bool,
    ) -> None:
        await events.publish_output(
            source=source,
            channel="error" if stage == "failure" else "text",
            text=summary,
            tool=REPORT_TASK_UNDERSTANDING_TOOL if source == "tool" else None,
            persist=persist,
            session_id=session_id,
            scope=TASK_UNDERSTANDING_SCOPE,
            scope_id=scope_id,
        )

    services = ResearchServices(
        infrastructure=ResearchInfrastructure(
            store=store,
            registry=registry,
            agents=agents,
            execution=execution,
            git=git,
            events=events,
            scripts=scripts,
            evaluator=evaluator,
            baseline_authority=baseline_authority,
        ),
        durable=DurableResearch(tree=tree, state=state),
    )
    if broker is not None:
        generator = (
            DeterministicClarificationGenerator()
            if provider is None
            else LLMClarificationGenerator(
                provider,
                store,
                publish_clarification_progress,
            )
        )
        services.workflow.clarification = ClarificationController(
            ClarificationStore(paths.athena),
            broker,
            generator,
            session_id=config.session_id,
            progress_sink=publish_clarification_progress,
        )

    # Keep mutable, process-local values outside the composition root.
    session = ResearchSession(
        lifecycle=LifecycleSession(task_text=config.task),
        compute=ComputeSession(data_root=config.data_root, config=config.compute),
        options=RuntimeOptions(
            ideation=config.ideation,
            direction=config.direction,
            tolerance=config.tolerance,
            auto_validate=config.auto_validate,
            skip_validate=config.skip_validate,
        ),
    )
    if config.compute is not None and config.compute.remote:
        session.compute.pool = GpuPool(
            list(config.compute.hosts),
            placement=config.compute.placement,
            store=store,
            dataset_root=config.data_root,
        )
    return services, session


def wire_workflow(runtime: Any) -> None:
    """Connect phase runners, Supervisor, and event projection once."""
    config = runtime.config
    services = runtime.services
    agent_turns = AgentTurnRunner(runtime)
    phases = PhaseRunner(runtime)
    # The phase and agent facades share the runtime but Supervisor owns state.
    deps = SupervisorDeps(
        paths=SupervisorPaths(
            project_root=config.paths.root,
            state_path=config.paths.state,
            tree_path=config.paths.tree,
        ),
        runtime=SupervisorRuntime(
            store=services.infrastructure.store,
            agents=services.infrastructure.agents,
            workspaces=services.infrastructure.git,
        ),
        research=ResearchActions(
            plan=phases.run_plan_turn,
            supervisor=agent_turns.run_supervisor_turn,
            ideator=agent_turns.run_ideator_turn,
            general=agent_turns.run_general_turn,
        ),
        phases=PhaseActions(
            publish=services.infrastructure.events.publish_from_supervisor,
            prepare=phases.run_prepare_phase,
            prepare_resume_is_attested=phases.baseline_resume_is_attested,
            validation=phases.run_validation_phase,
            publish_agent_event=services.infrastructure.events.project_agent_event,
            on_plan_settled=runtime.release_lease,
            auto_validate=config.auto_validate,
            skip_validate=config.skip_validate,
        ),
        search=SearchServices(
            scheduler=Scheduler(),
            recovery=Recovery(),
            direction=config.direction,
            tolerance=config.tolerance,
        ),
    )
    supervisor = Supervisor(
        state=services.durable.state,
        tree=services.durable.tree,
        deps=deps,
    )
    services.workflow.agent_turns = agent_turns
    services.workflow.phases = phases
    services.workflow.supervisor = supervisor
    services.infrastructure.events.attach_supervisor(supervisor)


def register_supervisor(runtime: Any, provider: object) -> None:
    """Register Supervisor and Plan agent types against one runtime."""
    if runtime.provider is not None:
        raise ValueError("SupervisorAgent provider is already registered")

    # Register the long-lived coordinator with its read-only competition context.
    runtime.session.lifecycle.provider = provider
    supervisor_kaggle = build_kaggle_stack(
        download_root=runtime.root,
        artifacts=runtime.store,
        download=True,
    )
    ask_user_factory = (
        (lambda _thread, _turn: runtime.config.ask_user)
        if runtime.config.ask_user is not None
        else None
    )
    register_supervisor_agent(
        runtime.registry,
        provider=provider,
        artifacts=runtime.store,
        actions=runtime.supervisor,
        ask_user=ask_user_factory,
        kaggle_stack=supervisor_kaggle,
    )

    # Keep Plan registration lazy so later survey results join its tool set.
    register_plan_agent(
        runtime.registry,
        provider=provider,
        artifacts=runtime.store,
        workspace_for=runtime.supervisor.workspace_path,
        execution=runtime.execution,
        extra_tools=runtime.plan_tools,
    )


def plan_tools(runtime: Any) -> ToolRegistry | None:
    """Build the optional Kaggle and corpus tools used by Plan agents."""
    return _merged(runtime.kaggle_tools("plan"), runtime.corpus_tools())


def kaggle_stack(runtime: Any) -> KaggleStack:
    """Build and cache the Kaggle stack shared by research agents."""
    if runtime.session.options.kaggle is None:
        runtime.session.options.kaggle = build_kaggle_stack(
            download_root=runtime.root,
            artifacts=runtime.store,
            download=runtime.supervisor.kaggle_download,
        )
    return runtime.session.options.kaggle


def kaggle_tools(runtime: Any, agent_type: str) -> ToolRegistry | None:
    """Build the configured Kaggle tool subset for one agent type."""
    names = AGENT_KAGGLE_TOOLS.get(agent_type)
    if not names or not runtime.supervisor.kaggle_enabled:
        return None
    stack = runtime.kaggle_stack()
    if not stack.client.configured:
        return None
    return build_kaggle_tools(stack, names=names)


def ideator_tools(runtime: Any) -> Callable[[], ToolRegistry | None]:
    """Return a lazy provider for the active Ideator tool set."""

    def build() -> ToolRegistry | None:
        """Build the current Ideator registry when the provider is requested."""
        return _merged(
            runtime.kaggle_tools("ideator"),
            runtime.corpus_tools(for_ideation=True),
        )

    return build


def baseline_ideator_tools(runtime: Any) -> Callable[[], ToolRegistry]:
    """Return a lazy web-enabled provider for the baseline ideator only."""

    def build() -> ToolRegistry:
        """Build the baseline ideator registry when the provider is requested."""
        registry = _merged(
            runtime.kaggle_tools("ideator"),
            runtime.corpus_tools(for_ideation=True),
            build_web_tools(),
        )
        assert registry is not None
        return registry

    return build


def _merged(*registries: ToolRegistry | None) -> ToolRegistry | None:
    """Merge optional tool registries while preserving registration order."""
    present = [registry for registry in registries if registry is not None]
    if not present:
        return None
    merged = ToolRegistry()
    for registry in present:
        for spec in registry.specs:
            merged.register(registry.resolve(spec.name))
    return merged


def _load_state(config: ResearchConfig) -> ResearchState:
    paths = config.paths
    state = (
        ResearchState.load(paths.state)
        if paths.state.is_file()
        else ResearchState(
            status="IDLE",
            phase="PREPARE",
            search_limit=config.search.search_limit,
            concurrency=config.search.concurrency,
            ideator_count=config.search.ideator_count,
            hypotheses_per_ideator=config.search.hypotheses_per_ideator,
        )
    )
    # ``ResearchRuntime`` uses the default value when no CLI option is given,
    # so preserve a persisted budget for that case.  A non-default value is an
    # explicit override and becomes the new durable budget for this project.
    if (
        state.search_limit != config.search.search_limit
        and config.search.search_limit != SearchLimits().search_limit
    ):
        state.search_limit = config.search.search_limit
        state.save(paths.state)
    if state.eda_dir is not None:
        eda_path = Path(state.eda_dir)
        if not eda_path.is_absolute():
            eda_path = (paths.root / eda_path).resolve()
        if not eda_path.is_relative_to(paths.root):
            state.eda_dir = None
    if state.status == "FAILED":
        state.status = "IDLE"
    state.experiment_timeout_s = config.experiment_timeout_s
    if config.data_root is not None and state.data_root is None:
        state.data_root = str(config.data_root)
    return state


__all__ = [
    "baseline_ideator_tools",
    "build_services",
    "ideator_tools",
    "kaggle_stack",
    "kaggle_tools",
    "plan_tools",
    "register_supervisor",
    "wire_workflow",
]

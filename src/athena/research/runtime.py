"""Composition root for Athena's autonomous research Supervisor."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from athena.agents.plan_agent import register_plan_agent
from athena.agents.supervisor_agent import register_supervisor_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.execution.runtime import CommandResult, ExecutionRuntime
from athena.research.contracts import ValidationResult
from athena.research.evaluation import TrustedEvaluator
from athena.research.agent_turn_runner import AgentTurnRunner
from athena.research.phase_runner import PhaseRunner
from athena.research.runtime_events import RuntimeEvents
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.events import EventProjector
from athena.research.supervisor.experiment import PlanTurnResult
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor

logger = logging.getLogger(__name__)

# shell_command 展示文本的字符上限：超长命令居中截断，保留首尾（对齐 codex）。
_MAX_COMMAND_CHARS = 400

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
        model: str | None = None,
        client: Any = None,
        task: str = "",
        auto_seed_task: bool = False,
        search_limit: int = 10,
        concurrency: int = 4,
        auto_validate: bool = False,
        direction: Literal["maximize", "minimize"] = "maximize",
        tolerance: float = 0.0,
        ideation: Literal["gated", "baseline"] = "gated",
        prepare_phase: PreparePhase | None = None,
        validation_phase: ValidationPhase | None = None,
        plan_turn: Callable[[str, Any], Awaitable[PlanTurnResult]] | None = None,
    ) -> None:
        # 消融开关：``gated`` 走 Idea Generation 门禁，``baseline`` 走 main 原有的
        # "产出即入库"。输出契约与 prompt 在 agent 注册时绑定，故一路传到
        # register_ideator_agent，不只是出口处分支。
        self._ideation = ideation
        self._root = Path(project_root or ".").resolve()
        self._athena = self._root / ".athena"
        self._state_path = self._athena / "state.json"
        self._tree_path = self._athena / "research_tree.json"
        self._output_log_path = self._athena / "logs" / "output.jsonl"
        self._store = LocalArtifactStore(self._athena / "artifacts")
        self._events = EventProjector(self._store)
        self._events_bus = RuntimeEvents(
            events=self._events,
            store=self._store,
            output_log_path=self._output_log_path,
        )
        # 断点续传：恢复历史输出序列号，避免重启后新事件与重放历史 seq 冲突
        # 而被 TUI 去重丢弃。
        for record in self._events_bus.replay_output_events():
            seq = record.get("seq")
            if isinstance(seq, int):
                self._events.resume(seq)
        self._registry = AgentTypeRegistry()
        self._agents = AgentRuntime(
            type_registry=self._registry,
            project_root=self._root,
            rollout_dir=self._athena / "logs" / "agents",
        )
        self._execution = ExecutionRuntime(
            project_root=self._root,
            environment_root=self._root,
            store=self._store,
        )
        self._scripts = DataScriptRunner(
            store=self._store,
            workdir=self._athena / "runs",
        )
        self._evaluator = TrustedEvaluator(self._scripts)
        self._git = LocalGitWorkspace(
            self._athena / "repo",
            self._root / "workspaces",
            self._store.put_bytes,
        )
        self._tree = (
            ResearchTree.load(self._tree_path)
            if self._tree_path.is_file()
            else ResearchTree()
        )
        initial_phase = (
            "PREPARE"
            if prepare_phase is not None or task or auto_seed_task
            else "SEARCH"
        )
        self._state = (
            ResearchState.load(self._state_path)
            if self._state_path.is_file()
            else ResearchState(
                status="RUNNING",
                phase=initial_phase,
                search_limit=search_limit,
                concurrency=concurrency,
            )
        )
        # 断点续传保护：跨目录拷贝来的 state 会携带旧项目的 eda_dir，使 PREPARE
        # 工作区/EDA 目录落到别的项目。强制校验其属于当前 project_root，否则置空
        # 让 PREPARE 按本项目重建——本项目只保留自身信息，唯一允许跨目录的是数据集源。
        # eda_dir 存的是相对项目根的路径（见 _run_prepare_phase），先解析成绝对再校验。
        eda_dir = self._state.eda_dir
        if eda_dir is not None:
            eda_path = Path(eda_dir)
            if not eda_path.is_absolute():
                eda_path = (self._root / eda_dir).resolve()
            if not eda_path.is_relative_to(self._root):
                self._state.eda_dir = None
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._auto_seed_task = auto_seed_task
        self._provider: object | None = None
        self._task_text = task
        self._model = model
        self._client = client
        self._direction = direction
        self._prepare_phase = prepare_phase
        self._validation_phase = validation_phase

        async def unavailable_plan_turn(_plan_id: str, _state: Any) -> PlanTurnResult:
            raise RuntimeError("SEARCH Plan execution is not configured")

        self._plan_turn = plan_turn or unavailable_plan_turn
        self._agent_turns = AgentTurnRunner(self)
        self._phase_runner = PhaseRunner(self)
        self._supervisor = Supervisor(
            project_root=self._root,
            state=self._state,
            tree=self._tree,
            store=self._store,
            agents=self._agents,
            workspaces=self._git,
            scheduler=Scheduler(),
            recovery=Recovery(),
            evaluator_ref=self._baseline_evaluator_ref(),
            run_plan_turn=self._phase_runner.run_plan_turn,
            run_supervisor_turn=self._agent_turns.run_supervisor_turn,
            run_ideator_turn=self._agent_turns.run_ideator_turn,
            run_general_turn=self._agent_turns.run_general_turn,
            publish=self._events_bus.publish_from_supervisor,
            auto_validate=auto_validate,
            direction=direction,
            tolerance=tolerance,
            run_prepare_phase=self._phase_runner.run_prepare_phase,
            run_validation_phase=self._phase_runner.run_validation_phase,
            publish_agent_event=self._events_bus.project_agent_event,
        )
        self._events_bus.attach_supervisor(self._supervisor)
        if model is not None:
            self.register_supervisor(provider=ResponsesProvider(model, client=client))

    @property
    def state(self) -> ResearchState:
        """Return the Supervisor-owned durable state."""
        return self._supervisor.state

    @property
    def research_state(self) -> ResearchState:
        """Compatibility alias for the same Supervisor-owned state object."""
        return self.state

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

    def register_supervisor(self, *, provider: object) -> None:
        """Register the long-lived SupervisorAgent once."""
        if self._provider is not None:
            raise ValueError("SupervisorAgent provider is already registered")
        self._provider = provider
        register_supervisor_agent(
            self._registry,
            provider=provider,
            artifacts=self._store,
            actions=self._supervisor,
        )
        register_plan_agent(
            self._registry,
            provider=provider,
            artifacts=self._store,
            workspace_for=self._supervisor.workspace_path,
            execution=self._execution,
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
        self._task = asyncio.create_task(self._supervisor.start())
        self._started = True
        return self._task

    async def start_task(self, task: str) -> str:
        """Seed the research task and start PREPARE -> SEARCH -> VALIDATE.

        Fresh runs begin at PREPARE so a trusted baseline/SOTA is established
        before any SEARCH hypothesis can be proposed. Existing ``state.json``
        (resume) keeps its phase and starts via ``recover()``.
        """
        self._task_text = task
        if not self._started:
            if (
                self.tree.best_experiment_id() is None
                and self._state.phase != "PREPARE"
            ):
                self._state.phase = "PREPARE"
            await self.start()
        return self.state.status

    async def message(self, text: str) -> str:
        """Apply exact control commands or delegate ordinary prose unchanged.

        With ``auto_seed_task`` (TUI entry), the first ordinary message before
        ``start()`` seeds the research task and starts PREPARE, so a trusted
        baseline/SOTA exists before SEARCH proposes hypotheses.
        """
        command = text.strip()
        if command == "/stop":
            return await self._supervisor.request_stop()
        if command == "/pause":
            return await self._supervisor.pause()
        if command == "/resume":
            await self._ensure_started()
            return await self._supervisor.resume()
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
        for ready in self._events_bus._subscriber_ready.values():
            if not ready.done():
                ready.cancel()
        self._events_bus._subscriber_ready.clear()
        self._events_bus._subscribers.clear()
        await self._supervisor.stop()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._agents.aclose()


__all__ = ["ResearchRuntime"]

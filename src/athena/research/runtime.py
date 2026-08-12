"""Composition root for Athena's autonomous research Supervisor."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from athena.agents.general_agent import GeneralResult, register_general_agent
from athena.agents.ideator_agent import register_ideator_agent
from athena.agents.plan_agent import register_plan_agent
from athena.agents.prepare_agent import register_prepare_agent
from athena.agents.supervisor_agent import (
    MAX_PLAN_TURNS,
    SUPERVISOR_AGENT_ID,
    register_supervisor_agent,
)
from athena.agents.validate_agent import register_validate_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.contracts import ArtifactRef, new_id
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_models import Hypothesis, HypothesisBatch
from athena.core.research_tree import ResearchTree
from athena.execution.runtime import CommandResult, ExecutionContext, ExecutionRuntime
from athena.research.contracts import ValidationResult
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.events import (
    EventProjector,
    StateEvent,
    redact,
    sanitize_terminal_text,
)
from athena.research.supervisor.experiment import PlanRunner, PlanTurnResult
from athena.research.supervisor.prepare import PrepareResult, run_prepare_plan
from athena.research.supervisor.plans import wait_run_events
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler, count_search_attempts
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from athena.research.supervisor.validation import (
    ValidationDiffReview,
    ValidationInput,
    run_validation_plan,
    validation_key,
)
from athena.utils.single_turn_chat import single_turn_chat

logger = logging.getLogger(__name__)

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
        prepare_phase: PreparePhase | None = None,
        validation_phase: ValidationPhase | None = None,
        plan_turn: Callable[[str, Any], Awaitable[PlanTurnResult]] | None = None,
    ) -> None:
        self._root = Path(project_root or ".").resolve()
        self._athena = self._root / ".athena"
        self._state_path = self._athena / "state.json"
        self._tree_path = self._athena / "research_tree.json"
        self._store = LocalArtifactStore(self._athena / "artifacts")
        self._events = EventProjector(self._store)
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
            self._athena / "workspaces",
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
        self._subscribers: dict[str, EmitFn] = {}
        self._subscriber_ready: dict[str, asyncio.Task[object]] = {}
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
            run_plan_turn=self._run_plan_turn,
            run_supervisor_turn=self._run_supervisor_turn,
            run_ideator_turn=self._run_ideator_turn,
            run_general_turn=self._run_general_turn,
            publish=self._publish_from_supervisor,
            auto_validate=auto_validate,
            direction=direction,
            tolerance=tolerance,
            run_prepare_phase=self._run_prepare_phase,
            run_validation_phase=self._run_validation_phase,
            publish_agent_event=self._project_agent_event,
        )
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

    def subscribe(self, emit: EmitFn) -> str:
        """Subscribe and immediately receive one complete state snapshot."""
        subscription_id = new_id("research")
        self._subscribers[subscription_id] = emit
        result = emit("state", self._state_event().model_dump(mode="json"))
        if asyncio.iscoroutine(result):
            self._subscriber_ready[subscription_id] = asyncio.create_task(result)
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        self._subscribers.pop(subscription_id, None)
        ready = self._subscriber_ready.pop(subscription_id, None)
        if ready is not None and not ready.done():
            ready.cancel()

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
        event = self._events.output(
            source=source,
            channel=channel,
            text=text,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
        )
        await self._publish("output", event.model_dump(mode="json"))

    async def project_command_result(
        self,
        result: CommandResult,
        *,
        plan: str | None = None,
        tool: str = "shell_command",
    ) -> None:
        """Publish one completed command as exactly one safe output event."""
        artifact_ref = result.output_ref
        if artifact_ref is not None:
            full = await self._store.get_text(artifact_ref)
            safe_full = redact(sanitize_terminal_text(full))
            if safe_full != full:
                unsafe_ref = artifact_ref
                artifact_ref = await self._store.put_text(safe_full)
                await asyncio.to_thread(
                    self._store.path_for(unsafe_ref).unlink, missing_ok=True
                )
        event = await self._events.tool_output(
            stdout=result.stdout,
            stderr=result.stderr,
            plan=plan,
            tool=tool,
            artifact_ref=artifact_ref,
        )
        await self._publish("output", event.model_dump(mode="json"))

    async def _project_agent_event(
        self,
        plan: str,
        kind: str,
        _event_ref: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = data or {}
        if kind == "agent/text_delta":
            text = str(payload.get("delta") or payload.get("accumulated") or "")
            # 流式 deltas 常为纯空白（换行/缩进），显示无信息量且会刷出空行。
            if text.strip():
                await self.publish_output(
                    source="agent", channel="text", text=text, plan=plan
                )
        elif kind == "agent/function_call":
            name = str(payload.get("name") or "tool")
            await self.publish_output(
                source="agent",
                channel="text",
                text=f"{name}(...)",
                plan=plan,
                tool=name,
            )
        elif kind == "command/completed":
            await self.project_command_result(CommandResult(**payload), plan=plan)
        elif kind == "tool/end":
            tool = str(payload.get("tool") or "")
            path = payload.get("path")
            if tool not in {"read_file", "write_file"} or not isinstance(path, str):
                return
            plan_state = self.state.plans.get(plan)
            if plan_state is None or plan_state.kind != "SEARCH":
                return
            try:
                worktree = self._supervisor.workspace_path(plan)
            except KeyError:
                return
            text = f"{path}\nworktree: {worktree}"
            if tool == "write_file":
                root = worktree.resolve()
                written = (root / path).resolve()
                if written.is_relative_to(root) and written.is_file():
                    content = await asyncio.to_thread(
                        written.read_text, encoding="utf-8"
                    )
                    text = f"{text}\n\n{content}"
            event = await self._events.tool_output(
                stdout=text,
                plan=plan,
                tool=tool,
            )
            await self._publish("output", event.model_dump(mode="json"))

    async def _publish_from_supervisor(
        self, kind: Literal["output", "state"], payload: dict[str, object]
    ) -> None:
        if kind == "state":
            payload = self._state_event().model_dump(mode="json")
        await self._publish(kind, payload)

    async def _publish(self, kind: str, payload: dict[str, object]) -> None:
        if kind not in {"output", "state"}:
            raise ValueError("runtime events must be output or state")

        async def invoke(subscription_id: str, emit: EmitFn) -> None:
            try:
                ready = self._subscriber_ready.pop(subscription_id, None)
                if ready is not None:
                    await ready
                result = emit(kind, payload)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "subscriber %s failed handling %s", subscription_id, kind
                )

        await asyncio.gather(
            *(invoke(key, emit) for key, emit in list(self._subscribers.items()))
        )

    def _state_event(self) -> StateEvent:
        state = self.state
        plans = [
            {"id": plan_id, **plan.model_dump(mode="json")}
            for plan_id, plan in state.plans.items()
        ]
        successes = sum(
            experiment.status.value == "SUCCEEDED"
            for experiment in self.tree.experiments(kind="search")
        )
        sota_id = self.tree.best_experiment_id()
        sota = None
        if sota_id is not None:
            experiment = self.tree.get_experiment(sota_id)
            sota = {
                "experiment": sota_id,
                "metric": experiment.eval.primary if experiment.eval else None,
                "commit": experiment.commit,
            }
        waiting_ids = [
            plan_id
            for plan_id, plan in state.plans.items()
            if plan.turn_limit is not None and plan.turns_used >= plan.turn_limit
        ]
        pending = [
            {"id": h.id, "statement": h.statement}
            for h in self.tree.pending_hypotheses()
            if h.id is not None
        ]
        return StateEvent(
            status=state.status,
            phase=state.phase,
            plans=plans,
            search={
                "attempts": count_search_attempts(state, self.tree),
                "limit": state.search_limit,
                "successes": successes,
                "concurrency": state.concurrency,
            },
            sota=sota,
            waiting=(
                {"plans": waiting_ids, "reason": "turn_limit_exhausted"}
                if waiting_ids
                else None
            ),
            manual=state.manual_mode,
            pending=pending,
        )

    async def _run_supervisor_turn(self, text: str) -> str:
        if self._provider is None:
            raise RuntimeError("SupervisorAgent provider is not registered")
        request = {"content": text, "context_refs": []}
        if self._agents.has_agent(SUPERVISOR_AGENT_ID):
            run_id = await self._agents.followup(SUPERVISOR_AGENT_ID, request)
        else:
            _agent_id, run_id = await self._agents.create_root(
                "supervisor",
                request,
                agent_id=SUPERVISOR_AGENT_ID,
                name=SUPERVISOR_AGENT_ID,
            )
        summary = await self._agents.wait_run(run_id)
        if summary.response_ref is None:
            raise RuntimeError(summary.error or "SupervisorAgent turn failed")
        envelope = json.loads(summary.response_ref)
        result_ref = envelope.get("result_ref")
        if not isinstance(result_ref, str):
            raise RuntimeError("SupervisorAgent returned no result artifact")
        answer = json.loads(await self._store.get_text(result_ref))["answer"]
        await self.publish_output(source="supervisor", channel="text", text=str(answer))
        return str(answer)

    async def _run_ideator_turn(self, count: int) -> list[Hypothesis]:
        """Run up to three Ideators against the EDA directory concurrently.

        Ideator Agent 的工具绑定 EDA 工作区（自行探索），输出 HypothesisBatch；
        Supervisor 负责把它们注册进 ResearchTree。EDA 目录路径直接来自 supervisor
        持有的持久化 state（PREPARE 时写入），不做任何 resolve。
        """
        if self._provider is None:
            raise RuntimeError("Ideator requires a registered Agent provider")
        eda_dir = self._state.eda_dir
        if not eda_dir:
            raise RuntimeError("EDA workspace not captured; PREPARE must run first")
        if not self._registry.contains("ideator"):
            register_ideator_agent(
                self._registry,
                provider=self._provider,
                artifacts=self._store,
                workspace=Path(eda_dir),
                runtime=self._execution,
            )
        allocations = self._ideator_allocations(count)
        lane_results = await asyncio.gather(
            *(
                self._run_ideator_lane(f"ideator-{index}", target, Path(eda_dir))
                for index, target in enumerate(allocations, start=1)
            ),
            return_exceptions=True,
        )
        hypotheses: list[Hypothesis] = []
        failures: list[BaseException] = []
        for index, result in enumerate(lane_results, start=1):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failures.append(result)
                await self.publish_output(
                    source="agent",
                    channel="error",
                    text=f"Ideator {index} failed: {result}",
                    plan=f"ideator-{index}",
                )
            else:
                hypotheses.extend(result)
        # 全部 lane 都失败时不再中断 SEARCH：每条失败已作为 error 输出发布，
        # 返回空列表让调度器优雅收尾（搜索自然结束，用户可见错误后干预）。
        return hypotheses[:count]

    @staticmethod
    def _ideator_allocations(count: int) -> tuple[int, ...]:
        """Distribute one requested batch across at most three actual lanes."""
        if count <= 0:
            return ()
        worker_count = min(3, count)
        base, remainder = divmod(count, worker_count)
        return tuple(base + (index < remainder) for index in range(worker_count))

    async def _run_ideator_lane(
        self, label: str, target: int, eda_dir: Path
    ) -> list[Hypothesis]:
        """Run one independent Ideator and return its structured batch."""
        request = {
            "content": (
                f"Inspect the EDA workspace at {eda_dir} without modifying any "
                "files, then propose up to "
                f"{target} falsifiable hypotheses that could improve the primary "
                "metric. Return the hypotheses as structured output."
            ),
            "context_refs": [],
        }
        _agent_id, run_id = await self._agents.create_root(
            "ideator", request, name=label
        )
        summary = await wait_run_events(
            self._agents,
            run_id,
            lambda kind, ref, data: self._project_agent_event(label, kind, ref, data),
        )
        if summary.response_ref is None:
            raise RuntimeError(summary.error or "Ideator turn failed")
        envelope = json.loads(summary.response_ref)
        result_ref = envelope.get("result_ref")
        if not isinstance(result_ref, str):
            raise RuntimeError("Ideator returned no result artifact")
        batch = HypothesisBatch.model_validate_json(
            await self._store.get_text(result_ref)
        )
        return batch.hypotheses

    async def _run_general_turn(self, task: str) -> dict[str, object]:
        """Dispatch one General Agent rooted at the project and return its result."""
        if self._provider is None:
            raise RuntimeError("General Agent requires a registered Agent provider")
        if not self._registry.contains("general"):
            register_general_agent(
                self._registry,
                provider=self._provider,
                artifacts=self._store,
                project_root=self._root,
                runtime=self._execution,
            )
        request = {"content": task, "context_refs": []}
        _agent_id, run_id = await self._agents.create_root(
            "general", request, name="general"
        )
        summary = await wait_run_events(
            self._agents,
            run_id,
            lambda kind, ref, data: self._project_agent_event(
                "general", kind, ref, data
            ),
        )
        if summary.response_ref is None:
            raise RuntimeError(summary.error or "General Agent turn failed")
        envelope = json.loads(summary.response_ref)
        result_ref = envelope.get("result_ref")
        if not isinstance(result_ref, str):
            raise RuntimeError("General Agent returned no result artifact")
        result = GeneralResult.model_validate_json(
            await self._store.get_text(result_ref)
        )
        return result.model_dump()

    async def _run_plan_turn(self, plan_id: str, state: Any) -> PlanTurnResult:
        if self._plan_turn.__name__ != "unavailable_plan_turn":
            return await self._plan_turn(plan_id, state)
        plan_input = await self._supervisor.plan_input(plan_id)
        runner = PlanRunner(
            execution=self._execution,
            store=self._store,
            evaluator=self._evaluator,
            workspace=self._git,
            branch=self._supervisor.workspace(plan_id),
            context=ExecutionContext(
                project_root=self._root,
                workspace_root=self._supervisor.workspace_path(plan_id),
                environment_root=self._root,
                experiment_id=plan_id,
            ),
            direction=plan_input.direction,
        )
        return await runner.run_turn(plan_id, state, plan_input)

    async def _run_prepare_phase(self) -> PrepareResult:
        if self._prepare_phase is not None:
            return await self._prepare_phase()
        if self._provider is None:
            raise RuntimeError("PREPARE requires a registered Agent provider")
        base_commit = await self._git.init()
        workspace = await self._git.create(base_commit, "athena/prepare")
        # 只把 EDA 目录路径交给 supervisor 持有的持久化 state；EDA 结果不进 SEARCH。
        self._state.eda_dir = str(Path(workspace.path).resolve())
        self._state.save(self._state_path)
        if not self._registry.contains("prepare"):
            register_prepare_agent(
                self._registry,
                provider=self._provider,
                artifacts=self._store,
                workspace=Path(workspace.path),
                runtime=self._execution,
            )
        tree_ref = await self._store.put_text(
            json.dumps(self.tree.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        return await run_prepare_plan(
            agents=self._agents,
            scripts=self._scripts,
            evaluator=self._evaluator,
            git=self._git,
            workspace=workspace,
            execution=self._execution,
            store=self._store,
            tree_ref=tree_ref,
            task=self._task_text,
            max_turns=MAX_PLAN_TURNS,
            publish=lambda kind, ref, data: self._project_agent_event(
                "prepare", kind, ref, data
            ),
        )

    async def _run_validation_phase(
        self, sota_commit: str, metric: float
    ) -> ValidationResult:
        if self._validation_phase is not None:
            return await self._validation_phase(sota_commit, metric)
        if self._provider is None:
            raise RuntimeError("VALIDATE requires a registered Agent provider")
        evaluator_ref = self._supervisor.evaluator_ref
        if evaluator_ref is None:
            raise RuntimeError("VALIDATE requires a frozen evaluator")
        workspace = await self._git.create(sota_commit, "athena/validate")
        if not self._registry.contains("validate"):
            register_validate_agent(
                self._registry,
                provider=self._provider,
                artifacts=self._store,
                workspace=Path(workspace.path),
                runtime=self._execution,
            )
        sota_id = self.tree.best_experiment_id()
        if sota_id is None:
            raise RuntimeError("VALIDATE requires a trusted SOTA baseline")
        experiment = self.tree.get_experiment(sota_id)
        hypothesis = self.tree.get_hypothesis(experiment.hypothesis_id)
        sota_context = {
            "experiment_id": sota_id,
            "statement": hypothesis.statement,
            "intervention": hypothesis.intervention,
            "expected_effect": hypothesis.expected_effect,
            "commit": experiment.commit,
            "metric": experiment.eval.primary if experiment.eval else None,
        }
        frozen = ValidationInput(
            sota_commit=sota_commit,
            reference_metric=metric,
            direction=self._direction,
            final_evaluator_ref=evaluator_ref,
            validation_key=validation_key(
                sota_commit, metric, self._direction, evaluator_ref
            ),
            sota_context=sota_context,
        )
        result_ref = None
        if self.state.validation is not None:
            candidate = self.state.validation.get("result_ref")
            if isinstance(candidate, str):
                result_ref = candidate
        return await run_validation_plan(
            input=frozen,
            agents=self._agents,
            git=self._git,
            workspace=workspace,
            execution=self._execution,
            evaluator=self._evaluator,
            store=self._store,
            independent_review=self._review_validation_diff,
            result_ref=result_ref,
            checkpoint=self._supervisor.checkpoint_validation,
            publish=lambda kind, ref, data: self._project_agent_event(
                "validate", kind, ref, data
            ),
        )

    async def _review_validation_diff(self, prompt: str) -> ValidationDiffReview:
        if self._model is None:
            raise RuntimeError("independent validation review requires a model")
        answer = await single_turn_chat(
            prompt,
            model=self._model,
            client=self._client,
            system_prompt=(
                "Independently review the proposed VALIDATE diff. Accept only "
                "runtime compatibility repairs and reject changes to model, data, "
                "features, preprocessing, training, or final-label access. Return "
                'JSON only: {"accepted":true|false,"reason":"..."}.'
            ),
            max_turns=200,
        )
        return ValidationDiffReview.model_validate_json(answer)

    def _baseline_evaluator_ref(self) -> ArtifactRef | None:
        baselines = self._tree.experiments(kind="baseline")
        return baselines[0].plan.run_config_ref if baselines else None

    async def aclose(self) -> None:
        for ready in self._subscriber_ready.values():
            if not ready.done():
                ready.cancel()
        self._subscriber_ready.clear()
        self._subscribers.clear()
        await self._supervisor.stop()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._agents.aclose()


__all__ = ["ResearchRuntime"]

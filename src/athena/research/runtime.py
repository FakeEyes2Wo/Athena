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
from athena.agents.prepare_agent import register_evaluator_agent, register_prepare_agent
from athena.agents.supervisor_agent import (
    MAX_PLAN_TURNS,
    SUPERVISOR_AGENT_ID,
    SupervisorAnswer,
    register_supervisor_agent,
)
from athena.agents.validate_agent import register_validate_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_models import Hypothesis, HypothesisBatch
from athena.core.research_tree import ResearchTree
from athena.execution.runtime import CommandResult, ExecutionContext, ExecutionRuntime
from athena.research.contracts import DataScriptBundle, ValidationResult
from athena.research.evaluation import TrustedEvaluator
from athena.research.runtime_events import RuntimeEvents
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.events import EventProjector
from athena.research.supervisor.experiment import (
    PlanRunner,
    PlanTurnResult,
    load_agent_result,
)
from athena.research.supervisor.prepare import (
    PrepareResult,
    run_evaluator_plan,
    run_prepare_plan,
)
from athena.research.supervisor.plans import wait_run_events
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
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

# shell_command 展示文本的字符上限：超长命令居中截断，保留首尾（对齐 codex）。
_MAX_COMMAND_CHARS = 400

EmitFn = Callable[[str, dict[str, object]], Awaitable[None] | None]
PreparePhase = Callable[[], Awaitable[PrepareResult]]
ValidationPhase = Callable[[str, float], Awaitable[ValidationResult]]
PlanTurn = Callable[[str, ResearchState], Awaitable[PlanTurnResult]]


async def _read_eval_handoff(
    store: ArtifactStore, evaluator_ref: ArtifactRef | None
) -> str:
    """Read the evaluator ``HANDOFF.md`` from a frozen bundle (empty when absent).

    HANDOFF.md 是 eval 合同的权威自描述（predictions 设置格式 + 判定标准），
    随 ``_freeze_evaluator`` 冻结进 bundle 的源码树。系统不解析它，只在 SEARCH
    propose/hypothesis 阶段透传给 ideator 阅读；缺失或不可读时返回空串。
    """
    if evaluator_ref is None:
        return ""
    try:
        bundle = DataScriptBundle.model_validate_json(
            await store.get_text(evaluator_ref)
        )
    except (ValueError, OSError):
        return ""
    if bundle.tree_ref is None:
        return ""
    try:
        tree = json.loads(await store.get_text(bundle.tree_ref))
    except (ValueError, OSError):
        return ""
    handoff_ref = tree.get("HANDOFF.md")
    if not isinstance(handoff_ref, str):
        return ""
    try:
        return await store.get_text(handoff_ref)
    except (ValueError, OSError):
        return ""


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
            publish=self._events_bus.publish_from_supervisor,
            auto_validate=auto_validate,
            direction=direction,
            tolerance=tolerance,
            run_prepare_phase=self._run_prepare_phase,
            run_validation_phase=self._run_validation_phase,
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
        result = await load_agent_result(summary, self._store, SupervisorAnswer)
        if result is None:
            raise RuntimeError(summary.error or "SupervisorAgent turn failed")
        await self.publish_output(
            source="supervisor", channel="text", text=result.answer
        )
        return result.answer

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
        eda_path = Path(eda_dir)
        # 相对项目根的路径（新契约）解析为绝对；旧 state 遗留的绝对路径原样保留。
        if not eda_path.is_absolute():
            eda_path = (self._root / eda_dir).resolve()
        root = getattr(self, "_root", None)
        if not eda_path.is_dir() or (
            root is not None and not eda_path.is_relative_to(root)
        ):
            raise RuntimeError(
                "EDA workspace is stale or points outside this project "
                f"({eda_dir}); reset the project and re-run PREPARE"
            )
        eda_dir = str(eda_path)
        if not self._registry.contains("ideator"):
            register_ideator_agent(
                self._registry,
                provider=self._provider,
                artifacts=self._store,
                workspace=Path(eda_dir),
                runtime=self._execution,
            )
        allocations = self._ideator_allocations(count)
        events = getattr(self, "_events_bus", None)
        if events is not None:
            events.set_ideator_lanes(len(allocations))
            await events.publish_ideator_state()
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
        content = (
            f"Inspect the EDA workspace at {eda_dir} without modifying any "
            "files, then propose up to "
            f"{target} falsifiable hypotheses that could improve the primary "
            "metric. Return the hypotheses as structured output."
        )
        context_refs: list[ArtifactRef] = []
        handoff = await _read_eval_handoff(self._store, self._supervisor.evaluator_ref)
        if handoff:
            context_refs.append(
                await self._store.put_text(
                    json.dumps({"eval_handoff": handoff}, ensure_ascii=False)
                )
            )
            content += (
                "\n\nThe evaluator contract (predictions directory layout and "
                "scoring criteria) is attached as context; read it before "
                "proposing hypotheses."
            )
        request = {"content": content, "context_refs": context_refs}
        _agent_id, run_id = await self._agents.create_root(
            "ideator", request, name=label
        )
        summary = await wait_run_events(
            self._agents,
            run_id,
            lambda kind, ref, data: self._events_bus.project_agent_event(
                label, kind, ref, data
            ),
        )
        batch = await load_agent_result(summary, self._store, HypothesisBatch)
        if batch is None:
            raise RuntimeError(summary.error or "Ideator turn failed")
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
            lambda kind, ref, data: self._events_bus.project_agent_event(
                "general", kind, ref, data
            ),
        )
        result = await load_agent_result(summary, self._store, GeneralResult)
        if result is None:
            raise RuntimeError(summary.error or "General Agent turn failed")
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
        workspace = await self._git.create(base_commit, "athena/prepare", name="eda")
        # 只把 EDA 目录路径交给 supervisor 持有的持久化 state；EDA 结果不进 SEARCH。
        # 存相对项目根的路径而非绝对路径：state 才项目自包含，复制/迁移项目后
        # 不会残留指向旧项目（如 hell）的绝对 eda_dir。
        self._state.eda_dir = str(
            Path(workspace.path).resolve().relative_to(self._root.resolve())
        )
        self._state.save(self._state_path)
        # 步骤 1：evaluator agent 在 workspaces/evaluator/（普通目录，非 git worktree）
        # 写评估器并冻结成 DataScriptBundle，得到 evaluator_ref。
        evaluator_dir = Path(self._root / "workspaces" / "evaluator")
        if not self._registry.contains("evaluator"):
            register_evaluator_agent(
                self._registry,
                provider=self._provider,
                artifacts=self._store,
                workspace=evaluator_dir,
                runtime=self._execution,
            )
        evaluator_ref = await run_evaluator_plan(
            agents=self._agents,
            scripts=self._scripts,
            store=self._store,
            evaluator_dir=evaluator_dir,
            execution=self._execution,
            task=self._task_text,
            max_turns=MAX_PLAN_TURNS,
            publish=lambda kind, ref, data: self._events_bus.project_agent_event(
                "evaluator", kind, ref, data
            ),
        )
        # 步骤 2：experiment/prepare agent 在 EDA worktree 写 experiment 产物并用
        # 步骤 1 冻结的 bundle 可信打分，提交 baseline。
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
            evaluator=self._evaluator,
            git=self._git,
            workspace=workspace,
            execution=self._execution,
            store=self._store,
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            task=self._task_text,
            max_turns=MAX_PLAN_TURNS,
            publish=lambda kind, ref, data: self._events_bus.project_agent_event(
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
            publish=lambda kind, ref, data: self._events_bus.project_agent_event(
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

"""AgentKernel — 整棵根 Agent 树的生命周期事务与状态机（设计 §3）。

单一命令序列器：所有 spawn/send_message/followup/interrupt/close 与 Run
terminal commit 均经 ``_command_queue`` 单任务处理；线性化点为
``AgentGraphStore.commit``。模型执行在序列器外运行，以 run_finished 命令回送。
"""

import asyncio
import logging
from collections import deque
from uuid import uuid4
from dataclasses import dataclass, field
from typing import Any

from athena.core.agent_kernel.registry import AgentTypeRegistry
from athena.core.agent_kernel.session import (
    AgentSession,
    InMemoryResourcesFactory,
    RunSession,
    SessionResourcesFactory,
)
from athena.core.agent_kernel.store import (
    AgentGraphStore,
    AgentRecord,
    CommandResult,
    JournalRecord,
    OutboxRecord,
    RunRecord,
    StoreSnapshot,
    WaitRecord,
)
from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentBusyError,
    AgentCommandError,
    AgentEvent,
    AgentId,
    AgentMessage,
    AgentPath,
    AgentSnapshot,
    AgentSpec,
    AgentStatus,
    AgentWaitResult,
    ArtifactRef,
    ErrorCode,
    ReturnWhen,
    RunId,
    RunStatus,
    RunSummary,
)

logger = logging.getLogger(__name__)


class AgentScheduler:
    """全局并发限制 + 按 agent_id 的 FIFO ready 队列（设计 §6）。

    同一 Agent 同时最多一个非终态 Run，ready 队列以 agent_id 为元素；
    dispatcher 取出后读取其 pending_run_id 再启动 turn。逻辑 Agent 总数由
    项目预算限制，不由本 Scheduler 计算；IDLE/WAITING 不占执行槽。
    """

    def __init__(self, *, max_active_agents: int) -> None:
        if max_active_agents <= 0:
            raise ValueError("capacity must be positive")
        self._max_active_agents = max_active_agents
        self._active: set[AgentId] = set()
        self._ready: deque[AgentId] = deque()
        self._lease_available = asyncio.Event()

    @property
    def max_active_agents(self) -> int:
        """全局同时执行上限。"""
        return self._max_active_agents

    @property
    def active_count(self) -> int:
        """当前占用执行槽的 Agent 数。"""
        return len(self._active)

    def enqueue(self, agent_id: AgentId) -> None:
        """将 Agent 放入 ready 队列（FIFO 尾）。"""
        self._ready.append(agent_id)

    def dequeue(self, agent_id: AgentId) -> None:
        """从 ready 队列移除 Agent；已派发则忽略。"""
        try:
            self._ready.remove(agent_id)
        except ValueError:
            # 中断 QUEUED 时可能已派发 → 无需移除
            pass

    def try_dispatch(self) -> AgentId | None:
        """有容量且 ready 非空时派发一个 Agent 并占用执行槽。"""
        if len(self._active) >= self._max_active_agents:
            return None
        while self._ready:
            agent_id = self._ready.popleft()
            self._active.add(agent_id)
            return agent_id
        return None

    def release_active(self, agent_id: AgentId) -> None:
        """释放执行槽并唤醒等待容量的 acquire_lease。"""
        self._active.discard(agent_id)
        self._lease_available.set()

    def park(self, agent_id: AgentId) -> None:
        """释放执行槽等待目标；公共 RunStatus 仍为 RUNNING（设计 §7.2）。"""
        self._active.discard(agent_id)
        self._lease_available.set()

    async def acquire_lease(self, agent_id: AgentId) -> None:
        """阻塞等待容量后重新占用执行槽（PARKED 恢复路径）。"""
        while len(self._active) >= self._max_active_agents:
            self._lease_available.clear()
            if len(self._active) < self._max_active_agents:
                break
            await self._lease_available.wait()
        self._active.add(agent_id)


class AgentRegistry:
    """父子树与展示路径查询。agent_id 为不透明 ID，身份以 parent_id 表达。

    路径由 store 记录派生，只用于展示和 list 过滤，不参与调度或定位。
    """

    def __init__(self, store: AgentGraphStore) -> None:
        self._store = store

    def child_path(self, parent_id: AgentId | None, name: str) -> AgentPath:
        """计算子 Agent 的展示路径；parent 以 store 记录查取，不拼接 id。"""
        if not name or "/" in name:
            raise ValueError("agent name must be non-empty and contain no '/'")
        if parent_id is None:
            return (name,)
        parent = self._store.agent(parent_id)
        if parent is None:
            raise KeyError(f"unknown parent: {parent_id}")
        return (*parent.path, name)

    def children(self, agent_id: AgentId) -> list[AgentId]:
        """返回 parent 的直接子 Agent id（排序）。"""
        return sorted(
            a.agent_id for a in self._store.agents().values() if a.parent_id == agent_id
        )

    def post_order(self, agent_id: AgentId) -> list[AgentId]:
        """后序遍历子树：先子孙后自身。"""
        result: list[AgentId] = []
        for child in self.children(agent_id):
            result.extend(self.post_order(child))
        result.append(agent_id)
        return result

    def has_live_descendants(self, agent_id: AgentId) -> bool:
        """子树中是否有非 CLOSED 的活子孙。"""
        return any(
            self._store.require_agent(c).status != AgentStatus.CLOSED
            for c in self.post_order(agent_id)[:-1]
        )

    def is_under_fence(self, agent_id: AgentId, fences: set[AgentId]) -> bool:
        """agent_id 自身或任一祖先在 fences 中 → 处于关闭中的子树。"""
        current: AgentId | None = agent_id
        while current is not None:
            if current in fences:
                return True
            agent = self._store.agent(current)
            current = agent.parent_id if agent is not None else None
        return False


@dataclass
class KernelCommand:
    """经序列器处理的一条命令。reply 在事务后解析。"""

    command_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    reply: asyncio.Future[Any] = field(default_factory=asyncio.Future)


class AgentKernel:
    """统一 Agent 底层。内部类型，不进入日常业务调用面。"""

    def __init__(
        self,
        *,
        resources_factory: SessionResourcesFactory | None = None,
        type_registry: AgentTypeRegistry | None = None,
        max_active_agents: int = 8,
        max_spawn_depth: int = 4,
        store: AgentGraphStore | None = None,
    ) -> None:
        self._resources_factory = resources_factory or InMemoryResourcesFactory()
        self._store = store or AgentGraphStore()
        self._type_registry = type_registry or AgentTypeRegistry()
        self._registry = AgentRegistry(self._store)
        self._scheduler = AgentScheduler(max_active_agents=max_active_agents)
        self._max_spawn_depth = max_spawn_depth
        self._sessions: dict[AgentId, AgentSession] = {}
        self._runner_tasks: dict[RunId, asyncio.Task[None]] = {}
        self._command_queue: asyncio.Queue[KernelCommand] = asyncio.Queue()
        self._command_results: dict[str, asyncio.Future[Any]] = {}
        self._run_waiters: dict[RunId, asyncio.Future[RunSummary]] = {}
        self._fences: set[AgentId] = set()
        self._serializer_task: asyncio.Task[None] | None = None
        self._park_locks: dict[RunId, asyncio.Lock] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._fatal: bool = False
        self._closed = False
        self._paused = False

    async def start(self) -> None:
        """启动命令序列器；随后派发已入队的 Run（含恢复的 QUEUED Run）。"""
        if self._closed or self._fatal:
            raise AgentCommandError(ErrorCode.CLOSED, "kernel unavailable")
        if self._serializer_task is not None:
            return
        self._serializer_task = asyncio.create_task(
            self._serializer_loop(), name="agent-kernel-serializer"
        )
        self._pump_scheduler()

    def pause(self) -> None:
        """停止派发新 Agent turn；运行中的到安全边界停下，保留 mailbox 与 wait（§13）。"""
        self._paused = True

    def resume(self) -> None:
        """恢复 ready Agent 的 FIFO 派发。"""
        self._paused = False
        self._pump_scheduler()

    async def aclose(self) -> None:
        """不可逆关闭：排空命令队列、关闭整棵树、停止序列器并等待 runner 静止。

        并发/重复 aclose 共享同一关闭 Task；每个调用者经 ``asyncio.shield`` 等待，
        任一调用者被取消不会毒化其余调用者（R7）。
        """
        if self._close_task is None:
            self._close_task = asyncio.get_event_loop().create_task(
                self._shutdown(), name="agent-kernel-close"
            )
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        """执行关闭流程；异常向所有等待者一致传播（warning 2）。"""
        self._closed = True
        self._drain_command_queue()
        tasks = list(self._runner_tasks.values())  # 关闭前冻结 runner 任务集
        for agent_id in list(self._store.agents()):
            self._close_one(agent_id)
        await self._stop_serializer()
        for task in tasks:
            # _close_one 已取消的任务不重复 cancel，避免二次取消打断 runner 清理（B5）
            if task.done() or task.cancelling() > 0:
                continue
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runner_tasks.clear()
        await self._resources_factory.aclose()

    def _drain_command_queue(self) -> None:
        """关闭前排空命令队列，未决命令以 CLOSED 拒绝。"""
        while True:
            try:
                command = self._command_queue.get_nowait()
            except asyncio.QueueEmpty:
                # 队列已空 → 排空完成
                break
            if command.kind == "kernel_shutdown":
                continue
            if not command.reply.done():
                command.reply.set_exception(
                    AgentCommandError(ErrorCode.CLOSED, "kernel closed")
                )

    async def _stop_serializer(self) -> None:
        """取消并等待序列器结束。"""
        if self._serializer_task is None:
            return
        self._serializer_task.cancel()
        try:
            await self._serializer_task
        except asyncio.CancelledError:
            # 序列器被取消 → 预期行为，忽略
            pass
        self._serializer_task = None

    async def _serializer_loop(self) -> None:
        while True:
            command = await self._command_queue.get()
            if command.kind == "kernel_shutdown":
                return
            try:
                result = self._dispatch(command)
                self._store.record_result(
                    command.command_id, CommandResult(value=result)
                )
                if not command.reply.done():
                    command.reply.set_result(result)
            except AgentCommandError as exc:
                # 领域错误 → 作为失败结果记录（可幂等重试），reply 抛回调用方
                self._store.record_result(command.command_id, CommandResult(error=exc))
                if not command.reply.done():
                    command.reply.set_exception(exc)
            except Exception as exc:
                # 未预期缺陷 → 记日志并抛回，避免序列器崩溃
                logger.exception("kernel command failed: %s", command.kind)
                if not command.reply.done():
                    command.reply.set_exception(exc)

    def _enqueue(
        self, kind: str, payload: dict[str, Any], command_id: str | None = None
    ) -> asyncio.Future[Any]:
        """入队命令并返回 reply future；同 command_id 重试返回原 future。"""
        if self._closed or self._fatal:
            raise AgentCommandError(ErrorCode.CLOSED, "kernel unavailable")
        cid = command_id or f"cmd:{uuid4().hex}:{kind}"
        future = self._command_results.get(cid)
        if future is not None:
            return future
        cached = self._store.result_for(cid)
        if cached is not None:
            future = asyncio.get_event_loop().create_future()
            self._command_results[cid] = future
            if cached.error is not None:
                future.set_exception(cached.error)
            else:
                future.set_result(cached.value)
            return future
        command = KernelCommand(command_id=cid, kind=kind, payload=payload)
        self._command_results[cid] = command.reply
        self._command_queue.put_nowait(command)
        return command.reply

    def _dispatch(self, command: KernelCommand) -> Any:
        kind = command.kind
        if kind == "spawn":
            return self._spawn(command)
        if kind == "send_message":
            self._send_message(command)
            return None
        if kind == "followup":
            return self._followup(command)
        if kind == "run_finished":
            self._run_finished(command)
            return None
        if kind == "interrupt":
            self._interrupt(command)
            return None
        if kind == "cancel_run":
            self._cancel_run(command)
            return None
        if kind == "close":
            self._close(command)
            return None
        if kind == "agent_error":
            self._agent_error(command)
            return None
        if kind == "agent_event":
            self._persist_event(command)
            return None
        if kind == "wait_for":
            self._wait_for(command)
            return None
        if kind == "wait_for_human":
            return self._wait_for_human(command)
        if kind == "human_reply":
            return self._human_reply(command)
        raise ValueError(f"unknown command kind: {kind}")

    async def persist_event(self, agent_id: AgentId, event: AgentEvent) -> None:
        """将事件经序列器耐久写入 Store（R3）。"""
        future = self._enqueue("agent_event", {"agent_id": agent_id, "event": event})
        await asyncio.shield(future)

    def _persist_event(self, command: KernelCommand) -> None:
        payload = command.payload
        self._store.commit(
            command_id=command.command_id,
            kind="agent_event",
            payload={"agent_id": payload["agent_id"], "event": payload["event"]},
        )

    async def create_root(
        self, agent_type: str, task: object, *, name: str = "root"
    ) -> tuple[AgentId, RunId]:
        """创建根 Agent 并返回 (agent_id, 首个 Run id)。agent_type 必须已注册。"""
        if not self._type_registry.contains(agent_type):
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, f"unknown agent_type: {agent_type}"
            )
        future = self._enqueue(
            "spawn",
            {"parent_id": None, "agent_type": agent_type, "task": task, "name": name},
        )
        return await asyncio.shield(future)

    async def spawn(
        self,
        parent_id: AgentId,
        agent_type: str,
        task: object,
        *,
        name: str | None = None,
    ) -> tuple[AgentId, RunId]:
        """在 parent 下创建子 Agent。agent_type 必须已注册。"""
        if not self._type_registry.contains(agent_type):
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, f"unknown agent_type: {agent_type}"
            )
        future = self._enqueue(
            "spawn",
            {
                "parent_id": parent_id,
                "agent_type": agent_type,
                "task": task,
                "name": name,
            },
        )
        return await asyncio.shield(future)

    def _spawn(self, command: KernelCommand) -> tuple[AgentId, RunId]:
        payload = command.payload
        parent_id = payload["parent_id"]
        agent_type = payload["agent_type"]
        name = payload["name"] or "agent"
        # parent 校验先于身份分配：失败零状态变更
        if parent_id is not None:
            parent = self._store.agent(parent_id)
            if parent is None:
                raise AgentCommandError(
                    ErrorCode.NOT_FOUND, f"unknown parent: {parent_id}"
                )
            if parent.status == AgentStatus.CLOSED:
                raise AgentCommandError(ErrorCode.CLOSED, f"parent closed: {parent_id}")
            if self._registry.is_under_fence(parent_id, self._fences):
                raise AgentCommandError(
                    ErrorCode.CLOSED, f"parent is closing: {parent_id}"
                )
            if len(parent.path) > self._max_spawn_depth:
                raise AgentCommandError(
                    ErrorCode.LIMIT_REACHED, "max_spawn_depth exceeded"
                )
        # name 与派生路径允许重复；身份唯一由不透明 agent_id 保证（设计 §5.1）
        path = self._registry.child_path(parent_id, name)
        agent_id = f"agent_{uuid4().hex[:8]}"  # 不透明唯一 ID；path 仅用于展示
        # 每实例 binding：factory 按 agent_id 创建全新 runner，同类型多实例不共享状态
        spec = self._type_registry.require_spec(agent_type, agent_id=agent_id)
        # 可失败步骤（资源、编码）先于提交；失败只产生可回收的孤儿资源
        resources = self._resources_factory.create(agent_id, spec)
        codec_error: str | None = None
        try:
            request_ref = spec.codec.encode_request(payload["task"])
        except Exception as exc:
            # 编解码失败 → 原异常只入受保护日志，不进入公开异常链（R6）
            logger.warning("codec encode failed: %s", type(exc).__name__)
            codec_error = type(exc).__name__
        if codec_error is not None:
            raise AgentCommandError(ErrorCode.CODEC_ERROR, codec_error)
        session = AgentSession(
            agent_id=agent_id,
            spec=spec,
            resources=resources,
            store=self._store,
            kernel=self,
        )
        self._sessions[agent_id] = session
        run_id = f"{agent_id}:r:{self._store.sequence + 1}"
        parent_run_id = None
        if parent_id is not None:
            parent_run_id = self._store.require_agent(parent_id).pending_run_id
        self._store.commit(
            command_id=command.command_id,
            kind="spawn",
            payload={
                "agent": AgentRecord(
                    agent_id=agent_id,
                    path=path,
                    name=name,
                    agent_type=agent_type,
                    parent_id=parent_id,
                    status=AgentStatus.IDLE,
                    created_sequence=self._store.sequence + 1,
                    pending_run_id=run_id,
                    context_ref=session.context_ref,
                ),
                "run": RunRecord(
                    run_id=run_id,
                    agent_id=agent_id,
                    parent_run_id=parent_run_id,
                    status=RunStatus.QUEUED,
                    generation=0,
                    request_ref=request_ref,
                ),
            },
        )
        self._scheduler.enqueue(agent_id)
        self._pump_scheduler()
        return agent_id, run_id

    def _pump_scheduler(self) -> None:
        """派发 ready 队列中可运行的 Agent；无容量、空队列、fatal 或 paused 时返回。"""
        if self._fatal:
            return  # fatal 后停止调度（R2）
        if self._paused:
            return  # 项目 pause：停止派发新 turn，运行中的到安全边界停下（§13）
        while True:
            agent_id = self._scheduler.try_dispatch()
            if agent_id is None:
                return
            # 同一 Agent 同时最多一个非终态 Run，pending_run_id 即待执行 turn
            agent = self._store.require_agent(agent_id)
            run_id = agent.pending_run_id
            run = self._store.require_run(run_id)
            generation = run.generation + 1
            self._store.commit(
                command_id=f"disp:{run_id}:{generation}",
                kind="run_running",
                payload={"run_id": run_id, "generation": generation},
            )
            session = self._sessions[agent_id]
            session.set_active_run(run_id, generation)
            task = asyncio.create_task(
                self._execute_run(run_id, generation), name=f"run-{run_id}"
            )
            self._runner_tasks[run_id] = task
            task.add_done_callback(
                lambda t, rid=run_id, g=generation: self._on_run_done(t, rid, g)
            )

    async def _execute_run(self, run_id: RunId, generation: int) -> None:
        run = self._store.require_run(run_id)
        agent = self._store.require_agent(run.agent_id)
        session = self._sessions[run.agent_id]
        before = session.memory.snapshot()[0]
        # runner 拿到绑定本 Run 的受限视图：旧 generation 无法借用新 Run 活跃态（B5）
        run_session = RunSession(session, run_id, generation)

        async def emit(kind: str, event_ref: str, data=None) -> None:
            await run_session.append_event(kind, event_ref, data)

        try:
            request = session.spec.codec.decode_request(run.request_ref)
        except asyncio.CancelledError:
            # 解码被外部取消 → 原样传播，由外层按中断处理
            raise
        except Exception as exc:
            # 编解码失败 → codec 损坏：Run FAILED 且 Agent ERROR，拒绝继续执行（R1-7）
            session.memory.rollback(before)
            await self._finish_codec_failure(run_id, generation, agent, exc)
            return

        try:
            response = await session.spec.runner.run(
                request, session=run_session, emit=emit
            )
            response_ref = session.spec.codec.encode_response(response)
            outcome = {"status": RunStatus.COMPLETED, "response_ref": response_ref}
        except asyncio.CancelledError:
            # 外部取消（interrupt/close）→ 终态由中断方提交；runner 自行取消则补交（R1-5）
            session.memory.rollback(before)
            run = self._store.run(run_id)
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                raise
            try:
                self._enqueue(
                    "run_finished",
                    {
                        "run_id": run_id,
                        "generation": generation,
                        "status": RunStatus.INTERRUPTED,
                        "reason": "runner cancelled",
                    },
                )
            except AgentCommandError:
                pass  # 内核已关闭，终态由关闭流程处理
            raise
        except Exception as exc:
            # Runner 领域失败 → 终态 FAILED，Agent 回 IDLE；错误脱敏，只暴露类型（R1-8）
            session.memory.rollback(before)
            outcome = {"status": RunStatus.FAILED, "error": type(exc).__name__}
        # 终态提交：重试原始 outcome，瞬时故障不改变业务执行结果（R1）
        for attempt in range(3):
            try:
                future = self._enqueue(
                    "run_finished",
                    {"run_id": run_id, "generation": generation, **outcome},
                )
                await future
                return
            except Exception:
                # 终态提交失败 → 退避重试原始 outcome；耗尽后走 fatal 兜底
                if attempt < 2:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                # 持久失败 → fatal 兜底（journal 提交或保持一致性）
                self._mark_terminal_fatal(run_id, outcome.get("error", "unknown"))
                return

    async def _finish_codec_failure(
        self, run_id: RunId, generation: int, agent: AgentRecord, exc: Exception
    ) -> None:
        """codec 损坏：Run 记 FAILED、Agent 记 ERROR，均经序列器提交。"""
        logger.error(
            "codec failed for agent %s: %s", agent.agent_id, type(exc).__name__
        )
        run_future = self._enqueue(
            "run_finished",
            {
                "run_id": run_id,
                "generation": generation,
                "status": RunStatus.FAILED,
                "error": "codec_failure",
                "reason": type(exc).__name__,
            },
        )
        error_future = self._enqueue("agent_error", {"agent_id": agent.agent_id})
        await asyncio.gather(run_future, error_future, return_exceptions=True)

    def _on_run_done(
        self, task: asyncio.Task[None], run_id: RunId, generation: int
    ) -> None:
        """runner 结束钩子：核验终态，未终态则异步补交 FAILED（B4）。"""
        self._runner_tasks.pop(run_id, None)
        run = self._store.run(run_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return
        error = "runner_ended_without_terminal"
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                error = type(exc).__name__
        asyncio.create_task(
            self._reconcile_untracked_terminal(run_id, generation, error)
        )

    async def _reconcile_untracked_terminal(
        self, run_id: RunId, generation: int, error: str
    ) -> None:
        """runner 已终结但未提交终态 → 补交 FAILED；持久失败进入 fatal（B4）。"""
        run = self._store.run(run_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return
        for attempt in range(3):
            try:
                future = self._enqueue(
                    "run_finished",
                    {
                        "run_id": run_id,
                        "generation": generation,
                        "status": RunStatus.FAILED,
                        "error": error,
                        "reason": None,
                    },
                )
                await future
                return
            except Exception:
                # 补交命令失败 → 退避重试；耗尽重试后走 fatal 兜底
                if attempt < 2:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                self._mark_terminal_fatal(run_id, error)
                return

    def _mark_terminal_fatal(self, run_id: RunId, error: str) -> None:
        """持久补交失败 → 经 journal 提交终态（最后手段）；仍失败则保持 journal 一致并 fatal（B2）。"""
        logger.error("persistent terminal commit failure for run %s", run_id)
        run = self._store.run(run_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            self._enter_fatal()
            return
        self._scheduler.release_active(run.agent_id)
        terminal_event: AgentEvent | None = None
        session = self._sessions.get(run.agent_id)
        if session is not None:
            session.clear_active_run()
            terminal_event = session.build_terminal_event(
                run_id,
                "run_failed",
                f"athena-event:{run_id}",
                {"status": RunStatus.FAILED, "error": error, "reason": None},
            )
        try:
            self._store.commit(
                command_id=f"fatal:{run_id}:{run.generation}",
                kind="run_terminal",
                payload={
                    "run_id": run_id,
                    "status": RunStatus.FAILED,
                    "generation": run.generation,
                    "response_ref": None,
                    "error": error,
                    "reason": None,
                    "terminal_event": terminal_event,
                },
            )
        except Exception:
            # store 完全不可用 → 不就地改状态，避免 journal 分裂；进入 fatal
            self._enter_fatal()
            return
        self._park_locks.pop(run_id, None)
        summary = self._store.run_summary(run_id)
        if summary is not None:
            self._resolve_run_waiter(run_id, summary)
        if terminal_event is not None:
            session.publish_event(terminal_event)
        self._enter_fatal()

    def _enter_fatal(self) -> None:
        """进入 fatal：拒绝新命令、停止调度、确定性解析所有 waiter（R2）。"""
        self._fatal = True
        for rid, future in self._run_waiters.items():
            if future.done():
                continue
            run = self._store.run(rid)
            agent_id = run.agent_id if run is not None else ""
            future.set_result(
                RunSummary(
                    run_id=rid,
                    agent_id=agent_id,
                    status=RunStatus.FAILED,
                    response_ref=None,
                    error="kernel_fatal",
                    reason=None,
                )
            )
        self._run_waiters.clear()

    def _run_finished(self, command: KernelCommand) -> None:
        payload = command.payload
        run_id = payload["run_id"]
        run = self._store.run(run_id)
        if (
            run is None
            or run.generation != payload["generation"]
            or run.status in TERMINAL_RUN_STATUSES
        ):
            return  # CAS 失败：迟到信号或已终态，first-writer-wins
        status = payload["status"]
        session = self._sessions[run.agent_id]
        session.clear_active_run()
        kind = {
            RunStatus.COMPLETED: "run_completed",
            RunStatus.FAILED: "run_failed",
            RunStatus.INTERRUPTED: "run_interrupted",
        }[status]
        summary = RunSummary(
            run_id=run_id,
            agent_id=run.agent_id,
            status=status,
            response_ref=payload.get("response_ref"),
            error=payload.get("error"),
            reason=payload.get("reason"),
        )
        agent = self._store.agent(run.agent_id)
        outbox = None
        if (
            agent is not None
            and agent.parent_id is not None
            and self._store.outbox_for(run_id) is None
        ):
            outbox = OutboxRecord(
                child_run_id=run_id,
                child_agent_id=agent.agent_id,
                parent_agent_id=agent.parent_id,
                summary=summary,
            )
        # 先构造终态事件，随终态事务一并耐久提交，后发布唯一事件（B1/R3）
        terminal_event = session.build_terminal_event(
            run_id, kind, f"athena-event:{run_id}", payload
        )
        self._store.commit(
            command_id=command.command_id,
            kind="run_terminal",
            payload={
                "run_id": run_id,
                "status": status,
                "response_ref": payload.get("response_ref"),
                "error": payload.get("error"),
                "reason": payload.get("reason"),
                "outbox": outbox,
                "terminal_event": terminal_event,
                "context_ref": session.context_ref,  # 安全边界写回私有记忆引用
            },
        )
        session.publish_event(terminal_event)
        self._scheduler.release_active(run.agent_id)
        self._park_locks.pop(run_id, None)
        self._resolve_run_waiter(run_id, summary)
        self._deliver_child_completion(run_id, summary)
        # agents-wait 命中检测：子 Agent 终态可能唤醒等待中的父 Agent
        self._maybe_wake_agents_wait(run.agent_id)
        self._pump_scheduler()

    def _resolve_run_waiter(self, run_id: RunId, summary: RunSummary) -> None:
        future = self._run_waiters.pop(run_id, None)
        if future is not None and not future.done():
            future.set_result(summary)

    async def wait_run(self, run_id: RunId, timeout: float | None = None) -> RunSummary:
        """等待 Run 达到终态并返回其摘要。"""
        run = self._store.run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        future = self._run_waiters.setdefault(
            run_id, asyncio.get_event_loop().create_future()
        )
        if run.status in TERMINAL_RUN_STATUSES:
            summary = self._store.run_summary(run_id)
            if summary is not None:
                self._resolve_run_waiter(run_id, summary)
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)

    def run_events(self, run_id: RunId, after_sequence: int = 0):
        """返回单 Run 事件流（§2.3 run.events）。"""
        run = self._store.run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        return self._sessions[run.agent_id].run_events(run_id, after_sequence)

    def session_events(self, agent_id: AgentId, after_sequence: int = 0):
        """返回整条 Session journal（跨 Run），供 handle.events()（§2.3）。"""
        session = self._sessions.get(agent_id)
        if session is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return session.events(after_sequence)

    def run_summary(self, run_id: RunId) -> RunSummary | None:
        """返回 Run 摘要；不存在返回 None。"""
        return self._store.run_summary(run_id)

    def _close_one(self, agent_id: AgentId) -> None:
        """关闭单个 Agent：中断其 Run、关闭会话、写 CLOSED tombstone。"""
        agent = self._store.agent(agent_id)
        if agent is None or agent.status == AgentStatus.CLOSED:
            return
        run = self._store.run(agent.pending_run_id) if agent.pending_run_id else None
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            self._commit_interrupted(run, "agent closed")
        session = self._sessions.get(agent_id)
        if session is not None:
            session.close()
        self._store.commit(
            command_id=f"closed:{agent_id}",
            kind="agent_closed",
            payload={"agent_id": agent_id},
        )

    def _check_open(self, target_id: AgentId) -> None:
        agent = self._store.agent(target_id)
        if agent is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {target_id}")
        if agent.status in (AgentStatus.CLOSED, AgentStatus.ERROR):
            raise AgentCommandError(
                ErrorCode.CLOSED, f"agent {agent.status.value}: {target_id}"
            )
        if self._registry.is_under_fence(target_id, self._fences):
            raise AgentCommandError(ErrorCode.CLOSED, f"agent is closing: {target_id}")

    def _agent_error(self, command: KernelCommand) -> None:
        """codec 损坏等不可恢复错误 → Agent 进入 ERROR 终态。"""
        self._store.commit(
            command_id=command.command_id,
            kind="agent_error",
            payload={"agent_id": command.payload["agent_id"]},
        )

    def agent_status(self, agent_id: AgentId) -> AgentStatus | None:
        """返回 Agent 生命周期状态；不存在返回 None。"""
        agent = self._store.agent(agent_id)
        return agent.status if agent is not None else None

    @classmethod
    async def from_snapshot(
        cls,
        *,
        snapshot: StoreSnapshot,
        journal: list[JournalRecord],
        resources_factory: SessionResourcesFactory,
        type_registry: AgentTypeRegistry | None = None,
        max_active_agents: int = 8,
    ) -> "AgentKernel":
        """从快照 + journal 重建内核（设计 §10）。"""
        store = AgentGraphStore()
        store.load(snapshot, journal)
        kernel = cls(
            resources_factory=resources_factory,
            type_registry=type_registry,
            max_active_agents=max_active_agents,
            store=store,
        )
        kernel._rebuild_sessions()
        kernel._recover()
        return kernel

    def _rebuild_sessions(self) -> None:
        for agent_id, agent in self._store.agents().items():
            if agent.status == AgentStatus.CLOSED:
                continue
            try:
                spec = self._type_registry.require_spec(
                    agent.agent_type, agent_id=agent_id
                )
                resources = self._resources_factory.create(
                    agent_id, spec, context_ref=agent.context_ref
                )
            except Exception:
                # 资源/类型重建失败 → Agent 进入 ERROR，不阻断其它恢复
                self._store.commit(
                    command_id=f"err:{agent_id}",
                    kind="agent_error",
                    payload={"agent_id": agent_id},
                )
                continue
            session = AgentSession(
                agent_id=agent_id,
                spec=spec,
                resources=resources,
                store=self._store,
                kernel=self,
            )
            # 以 Store 持久化的事件 journal 回填 live journal（R3）
            session.seed_events(self._store.events(agent_id))
            self._sessions[agent_id] = session

    def _recover(self) -> None:
        for run_id, run in self._store.runs().items():
            if run.status == RunStatus.QUEUED:
                if run.agent_id not in self._sessions:
                    # 资源重建失败 → Agent ERROR；原子终结其 QUEUED Run（B6）
                    self._terminate_orphan_run(run_id, run)
                    continue
                self._scheduler.enqueue(run.agent_id)
            elif run.status == RunStatus.RUNNING:
                # crash 时仍 RUNNING 且无 terminal marker → FAILED（精确 generation CAS）
                session = self._sessions.get(run.agent_id)
                if session is not None:
                    session.interrupt()
                self._store.commit(
                    command_id=f"restart:{run_id}",
                    kind="run_terminal",
                    payload={
                        "run_id": run_id,
                        "status": RunStatus.FAILED,
                        "generation": run.generation,
                        "response_ref": None,
                        "error": "kernel_restarted",
                        "reason": None,
                    },
                )
                self._scheduler.release_active(run.agent_id)
                self._park_locks.pop(run_id, None)
                summary = self._store.run_summary(run_id)
                if summary is not None:
                    self._resolve_run_waiter(run_id, summary)
            elif run.status in TERMINAL_RUN_STATUSES:
                summary = self._store.run_summary(run_id)
                if summary is not None:
                    self._resolve_run_waiter(run_id, summary)
        # 重放未投递的 completion outbox（幂等，B7）；不在此派发
        for child_run_id, record in self._store.outbox().items():
            self._deliver_child_completion(child_run_id, record.summary)

    def _terminate_orphan_run(self, run_id: RunId, run: RunRecord) -> None:
        """无 Session 的孤儿 Run（Agent 已 ERROR）→ 经 journal 原子标 FAILED（B6）。"""
        self._store.commit(
            command_id=f"orphan:{run_id}",
            kind="run_terminal",
            payload={
                "run_id": run_id,
                "status": RunStatus.FAILED,
                "generation": run.generation,
                "response_ref": None,
                "error": "kernel_restarted_no_session",
                "reason": None,
            },
        )
        self._park_locks.pop(run_id, None)
        summary = self._store.run_summary(run_id)
        if summary is not None:
            self._resolve_run_waiter(run_id, summary)

    def agent_snapshot(self, agent_id: AgentId) -> AgentSnapshot | None:
        """返回 Agent 元数据快照；不存在返回 None。"""
        return self._store.agent_snapshot(agent_id)

    def agent_path(self, agent_id: AgentId) -> AgentPath | None:
        """返回 Agent 展示路径；不存在返回 None。"""
        agent = self._store.agent(agent_id)
        return agent.path if agent is not None else None

    def registry_spec(self, agent_id: AgentId) -> AgentSpec:
        """按 agent_type 经注册表解析实例能力。"""
        agent = self._store.require_agent(agent_id)
        return self._type_registry.require_spec(agent.agent_type, agent_id=agent_id)

    def list_agents(self, path_prefix: AgentPath | None = None) -> list[AgentSnapshot]:
        """列出 Agent 快照，可按展示路径前缀过滤。"""
        return [
            snap
            for snap in (
                self._store.agent_snapshot(aid) for aid in self._store.agents()
            )
            if snap is not None
            and (not path_prefix or snap.path[: len(path_prefix)] == path_prefix)
        ]

    async def send_message(
        self,
        target_id: AgentId,
        message: str,
        context_refs: list[ArtifactRef] | None = None,
        *,
        source: str = "user",
    ) -> None:
        """向目标 mailbox 投递消息；只投递，不触发 Turn（设计 §3.3、§4.3）。

        ``source`` 为权威消息来源：用户/应用入口为保留值 ``"user"``，Agent 工具
        调用由 Kernel 按调用方 agent_id 填写。
        """
        future = self._enqueue(
            "send_message",
            {
                "target_id": target_id,
                "message": message,
                "context_refs": context_refs or [],
                "source": source,
            },
        )
        await asyncio.shield(future)

    def _send_message(self, command: KernelCommand) -> None:
        payload = command.payload
        target_id = payload["target_id"]
        self._check_open(target_id)
        # Kernel 生成权威 envelope：source 由调用方来源决定，消息原文与引用原样保留（B9）
        message = AgentMessage(
            source=payload.get("source", "user"),
            content=payload["message"],
            context_refs=payload.get("context_refs") or [],
        )
        self._store.commit(
            command_id=command.command_id,
            kind="mailbox",
            payload={"agent_id": target_id, "message": message},
        )

    def _deliver_child_completion(self, run_id: RunId, summary: RunSummary) -> None:
        """向父 mailbox 投递完成通知。

        outbox 已在 run_terminal 提交中原子写入，此处以 outbox_for 去重，
        只负责父 mailbox 通知（非关键状态）。
        """
        run = self._store.require_run(run_id)
        agent = self._store.require_agent(run.agent_id)
        parent_id = agent.parent_id
        if parent_id is None or self._store.outbox_for(run_id) is None:
            return  # 无父或 outbox 未写入 → 不投递
        if self._store.require_agent(parent_id).status == AgentStatus.CLOSED:
            return  # 父已关闭 → 只留审计记录，不投递 mailbox
        # 幂等：父 mailbox 已有同 run_id 的完成通知 → 不重复投递（B7，恢复重放安全）
        content = f"child completed: {run_id}"
        if any(
            m.source is None and m.content == content
            for m in self._store.mailbox(parent_id)
        ):
            return
        message = AgentMessage(
            source=None,
            content=content,
            context_refs=[summary.response_ref] if summary.response_ref else [],
        )
        self._store.commit(
            command_id=f"mailbox:{run_id}",
            kind="mailbox",
            payload={"agent_id": parent_id, "message": message},
        )

    async def wait_agent(
        self,
        target_ids: list[AgentId],
        *,
        return_when: ReturnWhen = ReturnWhen.FIRST_COMPLETED,
        timeout: float | None = None,
    ) -> AgentWaitResult:
        """等待目标当前 Run 终结；timeout 返回部分状态，不取消目标（§3.6）。"""
        frozen: dict[AgentId, RunId | None] = {}
        for target_id in target_ids:
            agent = self._store.agent(target_id)
            if agent is None:
                raise AgentCommandError(
                    ErrorCode.NOT_FOUND, f"unknown agent: {target_id}"
                )
            run_id = agent.pending_run_id
            run = self._store.run(run_id) if run_id else None
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                frozen[target_id] = None  # idle → 立即完成
            else:
                frozen[target_id] = run_id
        completed: dict[AgentId, RunSummary] = {}
        waits: dict[AgentId, asyncio.Task[RunSummary]] = {}
        for target_id, run_id in frozen.items():
            if run_id is None:
                summary = self._last_terminal_summary(target_id)
                if summary is not None:
                    completed[target_id] = summary  # idle → 立即完成
                continue
            run = self._store.require_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                summary = self._store.run_summary(run_id)
                if summary is not None:
                    completed[target_id] = summary
            else:
                waits[target_id] = asyncio.create_task(self.wait_run(run_id))
        if not waits:
            return AgentWaitResult(completed=completed, timed_out=False)
        if return_when == ReturnWhen.FIRST_COMPLETED and completed:
            # 冻结时已有目标终结 → FIRST 条件已满足，立即返回且不算超时（§3.6）
            for task in waits.values():
                task.cancel()
            return AgentWaitResult(completed=completed, timed_out=False)
        flag = (
            asyncio.FIRST_COMPLETED
            if return_when == ReturnWhen.FIRST_COMPLETED
            else asyncio.ALL_COMPLETED
        )
        try:
            done, pending = await asyncio.wait(
                waits.values(), return_when=flag, timeout=timeout
            )
        except asyncio.CancelledError:
            # 外层等待被取消 → 清理 helper tasks 后传播（B11）
            for task in waits.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waits.values(), return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        if waits:
            await asyncio.gather(*waits.values(), return_exceptions=True)
        for task in done:
            target_id = next(t for t, tk in waits.items() if tk is task)
            completed[target_id] = task.result()
        # ALL_COMPLETED：仍有 pending → 超时；FIRST_COMPLETED：无目标完成 → 超时（B9）
        timed_out = (
            bool(pending) if return_when == ReturnWhen.ALL_COMPLETED else not done
        )
        return AgentWaitResult(completed=completed, timed_out=timed_out)

    def _last_terminal_summary(self, agent_id: AgentId) -> RunSummary | None:
        """该 Agent 最近一次终态 Run 的摘要；idle 目标立即完成时使用（§3.6）。"""
        best: RunRecord | None = None
        for run in self._store.runs().values():
            if run.agent_id != agent_id or run.status not in TERMINAL_RUN_STATUSES:
                continue
            if best is None or self._run_sequence(run.run_id) > self._run_sequence(
                best.run_id
            ):
                best = run
        return self._store.run_summary(best.run_id) if best is not None else None

    def _run_sequence(self, run_id: RunId) -> int:
        """从 run_id 尾部 ``:r:`` 分隔的序号解析创建序列。"""
        return int(run_id.rsplit(":r:", 1)[-1])

    async def wait_agent_parked(
        self,
        target_ids: list[AgentId],
        *,
        parking_run_id: RunId | None,
        parking_generation: int | None = None,
        timeout: float | None = None,
    ) -> AgentWaitResult:
        """runner 经 session 等待：期间释放本 Run 的 lease，完成后恢复（§3.6）。

        同一 Run 的并发 parked wait 用每 Run 锁串行化，避免 park/reacquire
        相互等待造成自锁（R1-6）。``parking_generation`` 校验该 Run 仍为活跃执行
        实例，旧 RunSession 不得 park 新 Run（R5）。
        """
        if parking_run_id is None:
            return await self.wait_agent(target_ids, timeout=timeout)
        run = self._store.run(parking_run_id)
        if (
            run is None
            or run.status in TERMINAL_RUN_STATUSES
            or (parking_generation is not None and parking_generation != run.generation)
        ):
            return AgentWaitResult(
                completed={}, timed_out=False
            )  # 已终结/过期 → 立即返回
        lock = self._park_locks.setdefault(parking_run_id, asyncio.Lock())
        # 锁等待计入 timeout 预算；获取后只把剩余预算交给 wait_agent（B9）
        if timeout is None:
            await lock.acquire()
            remaining = None
        else:
            started = asyncio.get_running_loop().time()
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                # 锁等待超时 → 返回 timed_out，不抛异常（B8）
                return AgentWaitResult(completed={}, timed_out=True)
            remaining = timeout - (asyncio.get_running_loop().time() - started)
        try:
            self._scheduler.park(run.agent_id)
            self._pump_scheduler()
            try:
                return await self.wait_agent(target_ids, timeout=remaining)
            finally:
                await self._reacquire_lease(parking_run_id)
        finally:
            # 仅锁持有者释放；映射在 Run 终结时清理（B8）
            lock.release()

    async def _reacquire_lease(self, parking_run_id: RunId) -> None:
        """park 结束后恢复执行槽；被 park 的 Run 已终态则不再恢复（设计 §7.2）。"""
        run = self._store.run(parking_run_id)
        if run is None or run.status not in TERMINAL_RUN_STATUSES:
            return
        await self._scheduler.acquire_lease(run.agent_id)

    async def followup(self, agent_id: AgentId, task: object) -> RunId:
        """向 Agent 投递后续任务并返回新 Run id。"""
        future = self._enqueue("followup", {"target_id": agent_id, "task": task})
        return await asyncio.shield(future)

    def _followup(self, command: KernelCommand) -> RunId:
        payload = command.payload
        target_id = payload["target_id"]
        self._check_open(target_id)
        agent = self._store.require_agent(target_id)
        if agent.status in (AgentStatus.WAITING, AgentStatus.WAITING_FOR_HUMAN):
            raise AgentBusyError(f"agent waiting: {target_id}")
        pending = agent.pending_run_id
        run = self._store.run(pending) if pending else None
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            raise AgentBusyError(f"agent busy with run: {pending}")
        codec_error: str | None = None
        try:
            request_ref = self._type_registry.require_spec(
                agent.agent_type, agent_id=target_id
            ).codec.encode_request(payload["task"])
        except Exception as exc:
            # 编解码失败 → 原异常只入受保护日志，不进入公开异常链（R6）
            logger.warning("codec encode failed: %s", type(exc).__name__)
            codec_error = type(exc).__name__
        if codec_error is not None:
            raise AgentCommandError(ErrorCode.CODEC_ERROR, codec_error)
        run_id = f"{target_id}:r:{self._store.sequence + 1}"
        self._store.commit(
            command_id=command.command_id,
            kind="run_queued",
            payload={
                "run": RunRecord(
                    run_id=run_id,
                    agent_id=target_id,
                    parent_run_id=None,
                    status=RunStatus.QUEUED,
                    generation=0,
                    request_ref=request_ref,
                ),
            },
        )
        self._scheduler.enqueue(target_id)
        self._pump_scheduler()
        return run_id

    async def interrupt(self, target_id: AgentId, reason: str) -> None:
        """中断目标当前 Run；返回前 runner 已停止（§3.5）。"""
        agent = self._store.agent(target_id)
        run_id = agent.pending_run_id if agent else None
        # 入队前捕获：_commit_interrupted 会从 _runner_tasks 弹出并取消 task
        task = self._runner_tasks.get(run_id) if run_id else None
        future = self._enqueue("interrupt", {"target_id": target_id, "reason": reason})
        await asyncio.shield(future)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # 旧 runner 已取消 → 预期行为，忽略
                pass

    async def cancel_run(self, run_id: RunId, reason: str = "caller_cancelled") -> None:
        """按精确 run id 取消一个 Run；返回前 runner 已停止（§3.5）。"""
        run = self._store.run(run_id)
        task = self._runner_tasks.get(run_id) if run else None
        future = self._enqueue("cancel_run", {"run_id": run_id, "reason": reason})
        await asyncio.shield(future)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # 旧 runner 已取消 → 预期行为，忽略
                pass

    async def wait_for_human(
        self,
        agent_id: AgentId,
        content: str,
        context_refs: list[ArtifactRef] | None = None,
    ) -> str:
        """持久化登记人工等待并结束当前 turn（设计 §7.2）。返回稳定 request id。"""
        future = self._enqueue(
            "wait_for_human",
            {
                "agent_id": agent_id,
                "content": content,
                "context_refs": context_refs or [],
            },
        )
        return await asyncio.shield(future)

    def _validate_waiting(self, agent_id: AgentId) -> tuple[AgentRecord, RunRecord]:
        """等待前置校验：Agent 存在、非等待中、有活动 Run。"""
        agent = self._store.require_agent(agent_id)
        if agent.status in (AgentStatus.WAITING, AgentStatus.WAITING_FOR_HUMAN):
            raise AgentCommandError(
                ErrorCode.BUSY, f"agent already waiting: {agent_id}"
            )
        run = self._store.run(agent.pending_run_id) if agent.pending_run_id else None
        if run is None or run.status != RunStatus.RUNNING:
            raise AgentCommandError(
                ErrorCode.INVALID_REQUEST, f"no active run to wait on: {agent_id}"
            )
        return agent, run

    def _enter_wait(self, command_id: str, wait: WaitRecord, run: RunRecord) -> None:
        """提交 wait_enter 事务：Run 终态 + Agent 等待态 + 等待记录；释放执行槽。"""
        session = self._sessions[wait.agent_id]
        terminal_event = session.build_terminal_event(
            run.run_id,
            "run_completed",
            f"athena-event:{run.run_id}",
            {"wait": wait.kind},
        )
        self._store.commit(
            command_id=command_id,
            kind="wait_enter",
            payload={
                "run_id": run.run_id,
                "wait": wait,
                "generation": run.generation,
                "terminal_event": terminal_event,
                "context_ref": session.context_ref,  # 安全边界写回私有记忆引用
            },
        )
        session.publish_event(terminal_event)
        self._scheduler.release_active(wait.agent_id)
        self._resolve_run_waiter(
            run.run_id,
            RunSummary(
                run_id=run.run_id,
                agent_id=wait.agent_id,
                status=RunStatus.COMPLETED,
                response_ref=None,
            ),
        )
        self._pump_scheduler()  # 释放槽后派发排队中的 Agent

    def _wait_for_human(self, command: KernelCommand) -> str:
        """序列器处理器：持久化人工等待并结束当前 turn，返回稳定 request id。"""
        payload = command.payload
        agent_id = payload["agent_id"]
        _, run = self._validate_waiting(agent_id)
        request_id = f"human_{uuid4().hex[:8]}"
        wait = WaitRecord(
            agent_id=agent_id,
            kind="human",
            request_id=request_id,
            content=payload.get("content", ""),
            context_refs=payload.get("context_refs") or [],
        )
        self._enter_wait(command.command_id, wait, run)
        return request_id

    async def wait_for(self, agent_id: AgentId, target_ids: list[AgentId]) -> None:
        """持久化登记对一组 Agent 的依赖等待并结束当前 turn（设计 §7.2）。"""
        future = self._enqueue(
            "wait_for", {"agent_id": agent_id, "target_ids": list(target_ids)}
        )
        await asyncio.shield(future)

    def _wait_for(self, command: KernelCommand) -> None:
        payload = command.payload
        agent_id = payload["agent_id"]
        _, run = self._validate_waiting(agent_id)
        wait = WaitRecord(
            agent_id=agent_id, kind="agents", target_ids=list(payload["target_ids"])
        )
        self._enter_wait(command.command_id, wait, run)
        # 目标均已空闲 → 立即唤醒；否则等后续 completion 命中
        self._resolve_agents_wait(wait)

    async def human_reply(self, request_id: str, reply: str) -> RunId:
        """提交人工回复：写 mailbox、清除等待并创建新 turn 唤醒原 Agent（设计 §7.3）。

        返回唤醒后的新 Run id，便于调用方跟踪。
        """
        future = self._enqueue(
            "human_reply", {"request_id": request_id, "reply": reply}
        )
        return await asyncio.shield(future)

    def _human_reply(self, command: KernelCommand) -> RunId:
        payload = command.payload
        request_id = payload["request_id"]
        reply = payload["reply"]
        wait = self._store.wait_for_request(request_id)
        if wait is None:
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, f"unknown human wait: {request_id}"
            )
        agent_id = wait.agent_id
        self._store.commit(
            command_id=f"reply:{request_id}",
            kind="mailbox",
            payload={
                "agent_id": agent_id,
                "message": AgentMessage(source="user", content=str(reply)),
            },
        )
        return self._wake_waiting_agent(wait)

    def _wake_waiting_agent(self, wait: WaitRecord) -> RunId:
        """清除等待并为原 Agent 创建新 Run 入队（human 与 agents 共用，设计 §7.3）。"""
        agent_id = wait.agent_id
        agent = self._store.require_agent(agent_id)
        dedup_key = wait.request_id or wait.agent_id
        self._store.commit(
            command_id=f"resolve:{dedup_key}",
            kind="wait_resolve",
            payload={"agent_id": agent_id, "request_id": wait.request_id},
        )
        # 新 Run 只作唤醒触发；业务结果经 mailbox / 结果 Artifact 交接
        request_ref = self._type_registry.require_spec(
            agent.agent_type, agent_id=agent_id
        ).codec.encode_request({})
        run_id = f"{agent_id}:r:{self._store.sequence + 1}"
        self._store.commit(
            command_id=f"wake:{dedup_key}",
            kind="run_queued",
            payload={
                "run": RunRecord(
                    run_id=run_id,
                    agent_id=agent_id,
                    parent_run_id=None,
                    status=RunStatus.QUEUED,
                    generation=0,
                    request_ref=request_ref,
                ),
            },
        )
        self._scheduler.enqueue(agent_id)
        self._pump_scheduler()
        return run_id

    def _resolve_agents_wait(self, wait: WaitRecord) -> bool:
        """wait 的全部目标已完成 → 清除等待并创建唤醒 Run；返回是否已唤醒。"""
        for target_id in wait.target_ids:
            target = self._store.agent(target_id)
            if target is None:
                continue
            run = (
                self._store.run(target.pending_run_id)
                if target.pending_run_id
                else None
            )
            if run is not None and run.status not in TERMINAL_RUN_STATUSES:
                return False  # 尚有目标在运行
        self._wake_waiting_agent(wait)
        return True

    def _maybe_wake_agents_wait(self, child_agent_id: AgentId) -> None:
        """子 Agent 完成时，若命中父 Agent 的 agents-wait 且全部目标完成 → 唤醒一次。"""
        for wait in list(self._store.waits().values()):
            if wait.kind == "agents" and child_agent_id in wait.target_ids:
                self._resolve_agents_wait(wait)
                return  # 命中后等待已清除，多个 completion 只触发一次

    async def close(self, target_id: AgentId, *, recursive: bool = False) -> None:
        """关闭 Agent 子树；recursive=False 遇活子孙时失败（§3.5）。"""
        subtree = self._registry.post_order(target_id) if recursive else [target_id]
        runner_tasks = [
            task
            for agent_id in subtree
            if (agent := self._store.agent(agent_id)) is not None
            and agent.pending_run_id is not None
            and (task := self._runner_tasks.get(agent.pending_run_id)) is not None
        ]
        future = self._enqueue(
            "close", {"target_id": target_id, "recursive": recursive}
        )
        await asyncio.shield(future)
        for task in runner_tasks:
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # 旧 runner 已取消 → 预期行为，忽略
                    pass

    def _commit_interrupted(self, run: RunRecord, reason: str) -> None:
        session = self._sessions[run.agent_id]
        if run.status == RunStatus.QUEUED:
            self._scheduler.dequeue(run.agent_id)
        else:
            self._scheduler.release_active(run.agent_id)
        session.interrupt()
        summary = RunSummary(
            run_id=run.run_id,
            agent_id=run.agent_id,
            status=RunStatus.INTERRUPTED,
            response_ref=None,
            error=None,
            reason=reason,
        )
        agent = self._store.agent(run.agent_id)
        outbox = None
        if (
            agent is not None
            and agent.parent_id is not None
            and self._store.outbox_for(run.run_id) is None
        ):
            outbox = OutboxRecord(
                child_run_id=run.run_id,
                child_agent_id=agent.agent_id,
                parent_agent_id=agent.parent_id,
                summary=summary,
            )
        # 先构造终态事件，随终态事务一并耐久提交，后发布唯一事件（B1/R3）
        terminal_event = session.build_terminal_event(
            run.run_id,
            "run_interrupted",
            f"athena-event:{run.run_id}",
            {"reason": reason},
        )
        self._store.commit(
            command_id=f"int:{run.run_id}:{run.generation}",
            kind="run_terminal",
            payload={
                "run_id": run.run_id,
                "status": RunStatus.INTERRUPTED,
                "response_ref": None,
                "error": None,
                "reason": reason,
                "outbox": outbox,
                "terminal_event": terminal_event,
            },
        )
        session.publish_event(terminal_event)
        self._park_locks.pop(run.run_id, None)
        self._resolve_run_waiter(run.run_id, summary)
        self._deliver_child_completion(run.run_id, summary)
        self._maybe_wake_agents_wait(run.agent_id)
        task = self._runner_tasks.pop(run.run_id, None)
        if task is not None:
            task.cancel()

    def _interrupt(self, command: KernelCommand) -> None:
        payload = command.payload
        target_id = payload["target_id"]
        agent = self._store.agent(target_id)
        if agent is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {target_id}")
        if agent.status == AgentStatus.CLOSED:
            raise AgentCommandError(ErrorCode.CLOSED, f"agent closed: {target_id}")
        run_id = agent.pending_run_id
        run = self._store.run(run_id) if run_id else None
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return  # idle / 重复 interrupt → 幂等 no-op
        self._commit_interrupted(run, payload.get("reason", "interrupted"))
        self._pump_scheduler()

    def _cancel_run(self, command: KernelCommand) -> None:
        payload = command.payload
        run_id = payload["run_id"]
        run = self._store.run(run_id)
        if run is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown run: {run_id}")
        if run.status in TERMINAL_RUN_STATUSES:
            return
        self._commit_interrupted(run, payload.get("reason", "caller_cancelled"))
        self._pump_scheduler()

    def _close(self, command: KernelCommand) -> None:
        payload = command.payload
        target_id = payload["target_id"]
        recursive = payload.get("recursive", False)
        agent = self._store.agent(target_id)
        if agent is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {target_id}")
        if agent.status == AgentStatus.CLOSED:
            return  # 重复 close → 幂等 no-op
        if not recursive:
            if self._registry.has_live_descendants(target_id):
                raise AgentCommandError(
                    ErrorCode.INVALID_REQUEST,
                    "cannot close agent with live descendants; use recursive=True",
                )
        subtree = self._registry.post_order(target_id)
        self._fences.update(subtree)
        for aid in subtree:
            self._close_one(aid)
        self._fences.difference_update(subtree)
        self._pump_scheduler()  # 关闭释放的容量唤醒排队 Run（与 interrupt 一致）

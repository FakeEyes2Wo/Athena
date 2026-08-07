"""Thread 执行内核 — ThreadRuntime, ThreadHandle, SubmissionLoop。

Memory 接入：
- ThreadRuntime 拥有 ContextManager / Compactor / RolloutRecorder
- _run_turn() 通过 ThreadRuntime 方法访问 Memory，不直接操作内部状态
- maybe_compact() — Turn 前压缩检查
- record_items()    — Turn 后持久化到 JSONL
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from athena.app_server.events import Event, EventJournal
from athena.app_server.exceptions import ClosedError
from athena.app_server.observability import ThreadExecutionObserver
from athena.app_server.submissions import (
    GetForkSnapshot,
    InterruptTurn,
    RunnerCancelled,
    RunnerFailed,
    RunnerSucceeded,
    RuntimeSignal,
    ShutdownThread,
    StartTurn,
    Submission,
    TurnTerminalState,
    TurnRuntime,
)
from athena.core.contracts import ArtifactRef
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.execution import MonitorLimits

if TYPE_CHECKING:
    from athena.memory.compaction import Compaction, Compactor
    from athena.memory.context_manager import ContextManager
    from athena.memory.rollout import RolloutRecorder
    from pydantic_ai.messages import ModelMessage

logger = logging.getLogger(__name__)

EmitEvent = Callable[[str, ArtifactRef], Awaitable[None]]
Runner = Callable[
    [AthenaThread, AthenaTurn, EmitEvent], Awaitable[tuple[ArtifactRef, ArtifactRef]]
]


class _MergedQueue:
    """双队列合并为单流 — control 和 submission 共用一个消费点。"""

    def __init__(self, control_q: asyncio.Queue, submission_q: asyncio.Queue) -> None:
        self._ctrl = control_q
        self._sub = submission_q
        self._merged: asyncio.Queue[Submission | RuntimeSignal] = asyncio.Queue(20)
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._drain(self._ctrl), name="merged-ctrl"),
            asyncio.create_task(self._drain(self._sub), name="merged-sub"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def get(self) -> Submission | RuntimeSignal:
        return await self._merged.get()

    async def _drain(self, src: asyncio.Queue) -> None:
        while True:
            msg = await src.get()
            await self._merged.put(msg)


class ThreadRuntime:
    """单个 Thread 的执行容器 — 状态、通道、Journal、后台 loop。

    Memory 所有权：ThreadRuntime 拥有 ContextManager / Compactor / RolloutRecorder。
    通过 maybe_compact() / record_items() 封装 Memory 操作，
    _run_turn() 只在编排点调用这些方法，不直接操作内部状态。
    """

    def __init__(
        self,
        thread_id: str,
        session_id: str,
        context_ref: ArtifactRef,
        runner: Runner,
        *,
        submission_capacity: int = 16,
        ctx: "ContextManager | None" = None,
        compactor: "Compactor | None" = None,
        rollout: "RolloutRecorder | None" = None,
        llm: Any = None,
        monitor_limits: MonitorLimits | None = None,
        monitor_scan_interval: float = 1.0,
        monitor_terminal_retention: float = 300.0,
    ) -> None:
        self.thread_id = thread_id
        self.session_id = session_id
        self.context_ref = context_ref
        self._runner = runner
        self.state: Literal["idle", "running", "closing", "closed"] = "idle"

        # 记忆层
        self._ctx = ctx
        self._compactor = compactor
        self._rollout = rollout
        self._llm = llm

        # 双向通道
        self.submission_queue = asyncio.Queue[Submission](submission_capacity)
        self.control_queue = asyncio.Queue[RuntimeSignal](4)
        self._merged = _MergedQueue(self.control_queue, self.submission_queue)

        # 事件 & 并发
        self.journal = EventJournal(thread_id)
        self._execution_observer = ThreadExecutionObserver(
            self.journal,
            limits=monitor_limits,
            scan_interval=monitor_scan_interval,
            terminal_retention=monitor_terminal_retention,
        )
        self.loop_task: asyncio.Task[None] | None = None
        self.active_turn: TurnRuntime | None = None
        self._turn_done: dict[str, asyncio.Future[TurnTerminalState]] = {}

        # 快照
        self.active_turn_id: str | None = None
        self.last_terminal_kind: str | None = None
        self._completed_contexts: dict[str, ArtifactRef] = {}
        self._completed_results: dict[str, ArtifactRef] = {}

    async def start(self) -> None:
        """启动合并队列和 submission_loop 后台任务。"""
        if self.loop_task is not None:
            return
        if self._rollout is not None:
            await self._rollout.open(self.thread_id)
        await self._merged.start()
        self.loop_task = asyncio.create_task(
            submission_loop(self), name=f"submission-loop-{self.thread_id}"
        )
        await self._execution_observer.start()

    async def shutdown(self, reason: str) -> None:
        """优雅关闭：入队 ShutdownThread → 等待 loop 退出 → 停止合并队列。"""
        if self.state in ("closing", "closed"):
            return
        self.state = "closing"
        sub = Submission(id=str(uuid4()), op=ShutdownThread(reason=reason))
        try:
            self.submission_queue.put_nowait(sub)
        except asyncio.QueueFull:
            # 提交队列满 → 回退到阻塞 put，保证关闭信号不丢失
            await self.submission_queue.put(sub)
        if self.loop_task is not None:
            try:
                await self.loop_task
            except asyncio.CancelledError:
                # loop_task 在关闭流程中被取消 → 预期行为，忽略
                pass
        await self._merged.stop()
        await self._execution_observer.stop()
        self.state = "closed"
        if self._rollout is not None:
            await self._rollout.close()

    async def force_close(self) -> None:
        """强制关闭：取消活跃 turn runner + 取消 loop。"""
        self.state = "closing"
        if self.active_turn is not None:
            self.active_turn.cancel_requested.set()
            self.active_turn.runner_task.cancel()
        if self.loop_task is not None:
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                # 强制取消 loop_task → 预期行为，忽略
                pass
        for future in self._turn_done.values():
            if not future.done():
                future.set_result(TurnTerminalState(cancelled=True))
        if self.active_turn is not None:
            self._clear_active_turn(self.active_turn.turn_id)
        await self._merged.stop()
        await self._execution_observer.stop()
        self.state = "closed"
        if self._rollout is not None:
            await self._rollout.close()

    async def maybe_compact(self) -> "Compaction | None":
        """Turn 前压缩检查。超过阈值时用 LLM 摘要替换早期消息。

        空压缩（无旧消息）不记录 checkpoint；
        有摘要时重记录 compaction 后的 tail 到 rollout。
        """
        if self._compactor is None or self._ctx is None or self._llm is None:
            return None
        if not self._compactor.should_compact(self._ctx):
            return None
        ckpt = await self._compactor.compact(self._ctx, self._llm)
        if ckpt.original_items and self._rollout is not None:
            self._rollout.record_compaction(ckpt.version, ckpt.summary)
            tail = self._ctx.items_since(1)  # skip summary at index 0
            for msg in tail:
                self._rollout.record(msg)
        return ckpt

    def record_items(self, messages: list["ModelMessage"]) -> None:
        """持久化消息到 JSONL — 不重复追加到 _ctx（消息已在上下文中）。"""
        if self._rollout is None:
            return
        for msg in messages:
            self._rollout.record(msg)

    @property
    def has_memory(self) -> bool:
        return self._ctx is not None

    def _make_lifecycle_event(self, turn_id: str, kind: str) -> Event:
        return Event(
            thread_id=self.thread_id,
            turn_id=turn_id,
            sequence=self.journal.next_sequence(),
            kind=kind,
            event_ref=f"athena-event:{uuid4().hex}",
        )

    async def accept_turn(self, op: StartTurn) -> AthenaTurn:
        """登记 Turn 启动事件，将状态切换到 running。"""
        if self.active_turn is not None:
            raise RuntimeError("thread already has an active turn")
        turn = AthenaTurn(
            turn_id=op.turn_id,
            thread_id=self.thread_id,
            request_ref=op.request_ref,
            status="running",
        )
        async with self.journal.condition:
            self.journal.append(
                self._make_lifecycle_event(turn.turn_id, "turn_started")
            )
        self.state = "running"
        self.active_turn_id = turn.turn_id
        await self._execution_observer.turn_started(turn.turn_id)
        return turn

    def spawn_turn(self, turn: AthenaTurn) -> TurnRuntime:
        """创建 TurnRuntime 并启动 turn runner 任务。"""
        tr = TurnRuntime(
            turn_id=turn.turn_id,
            request_ref=turn.request_ref,
            runner_task=asyncio.create_task(
                _run_turn(self, turn), name=f"turn-{turn.turn_id}"
            ),
        )
        self.active_turn = tr
        self._turn_done[turn.turn_id] = tr.done
        return tr

    async def commit_completed(
        self, turn_id: str, result_ref: ArtifactRef, next_context_ref: ArtifactRef
    ) -> None:
        """记录 Turn 成功完成到 Journal 并更新上下文引用。"""
        async with self.journal.condition:
            if self.active_turn is None or self.active_turn.turn_id != turn_id:
                return
            self.journal.append(self._make_lifecycle_event(turn_id, "turn_completed"))
            self.context_ref = next_context_ref
            self._completed_contexts[turn_id] = next_context_ref
            self._completed_results[turn_id] = result_ref
            self.last_terminal_kind = "turn_completed"
            self._turn_done[turn_id].set_result(
                TurnTerminalState(
                    result_ref=result_ref, next_context_ref=next_context_ref
                )
            )
            self._clear_active_turn(turn_id)
        await self._execution_observer.turn_completed(turn_id)

    async def commit_failed(self, turn_id: str, exception_type: str) -> None:
        """记录 Turn 执行失败到 Journal。"""
        async with self.journal.condition:
            if self.active_turn is None or self.active_turn.turn_id != turn_id:
                return
            self.journal.append(self._make_lifecycle_event(turn_id, "turn_failed"))
            self.last_terminal_kind = "turn_failed"
            self._turn_done[turn_id].set_result(
                TurnTerminalState(exception_type=exception_type)
            )
            self._clear_active_turn(turn_id)
        await self._execution_observer.turn_failed(turn_id, exception_type)

    async def commit_interrupted(self, turn_id: str, reason: str) -> None:
        """记录 Turn 被中断到 Journal 并设置取消标记。"""
        async with self.journal.condition:
            if self.active_turn is None or self.active_turn.turn_id != turn_id:
                return
            self.active_turn.cancel_requested.set()
            self.journal.append(self._make_lifecycle_event(turn_id, "turn_interrupted"))
            self.last_terminal_kind = "turn_interrupted"
            self._turn_done[turn_id].set_result(TurnTerminalState(cancelled=True))
            self._clear_active_turn(turn_id)
        await self._execution_observer.turn_cancelled(turn_id)

    def _clear_active_turn(self, turn_id: str) -> None:
        if self.active_turn is not None and self.active_turn.turn_id == turn_id:
            self.active_turn = None
            if self.state == "running":
                self.state = "idle"
            self.active_turn_id = None

    def completed_snapshot(self, after_turn_id: str | None) -> ArtifactRef:
        """返回指定 Turn 或当前的上下文快照。"""
        if after_turn_id is not None:
            ctx = self._completed_contexts.get(after_turn_id)
            if ctx is None:
                raise KeyError(f"no completed snapshot: {after_turn_id}")
            return ctx
        return self.context_ref

    async def wait_turn(self, turn_id: str) -> ArtifactRef:
        try:
            done = self._turn_done[turn_id]
        except KeyError as exc:
            raise KeyError(f"unknown turn: {turn_id}") from exc
        terminal = await asyncio.shield(done)
        if terminal.cancelled:
            raise asyncio.CancelledError
        if terminal.exception_type is not None:
            raise RuntimeError(f"turn failed: {terminal.exception_type}")
        if terminal.result_ref is None:
            raise RuntimeError("completed turn has no result reference")
        return terminal.result_ref

    async def ensure_runner_stopped(self) -> None:
        """取消活跃 Turn 的 runner 任务并等待退出。"""
        if self.active_turn is not None:
            self.active_turn.runner_task.cancel()
            try:
                await self.active_turn.runner_task
            except asyncio.CancelledError:
                # runner_task 已被取消 → 预期行为，忽略
                pass

    def fail_pending_submissions(self, error: ClosedError) -> None:
        """排空待处理提交队列，对未完成的提交抛出 ClosedError。"""
        while True:
            try:
                sub = self.submission_queue.get_nowait()
                if not sub.reply.done():
                    sub.reply.set_exception(error)
            except asyncio.QueueEmpty:
                # 队列已排空 → 所有待处理提交已处理完毕
                break


class ThreadHandle:
    """ThreadRuntime 公开边界 — 不含 Queue/Task/锁。"""

    def __init__(self, runtime: ThreadRuntime) -> None:
        self.thread_id = runtime.thread_id
        self._runtime = runtime

    async def submit(
        self, op: StartTurn | InterruptTurn | GetForkSnapshot | ShutdownThread
    ) -> object:
        """向 ThreadRuntime 提交一个控制操作，返回操作的回复。"""
        if self._runtime.state in ("closing", "closed"):
            raise ClosedError("thread is closed")
        sub_id = op.turn_id if isinstance(op, StartTurn) else str(uuid4())
        sub = Submission(id=sub_id, op=op, reply=asyncio.Future())
        try:
            self._runtime.submission_queue.put_nowait(sub)
        except asyncio.QueueFull:
            # 提交队列满 → 回退到阻塞 put，保证提交不丢失
            await self._runtime.submission_queue.put(sub)
        return await sub.reply

    def events(self, *, after_sequence: int = 0) -> AsyncIterator[Event]:
        """返回从指定 sequence 之后开始的异步事件迭代器。"""
        return self._runtime.journal.read_from(after_sequence)

    async def shutdown_and_wait(self, reason: str = "client_shutdown") -> None:
        """优雅关闭 ThreadRuntime 并等待退出。"""
        await self._runtime.shutdown(reason)

    async def wait_turn(self, turn_id: str) -> ArtifactRef:
        return await self._runtime.wait_turn(turn_id)

    @property
    def state(self) -> str:
        return self._runtime.state

    @property
    def context_ref(self) -> ArtifactRef:
        return self._runtime.context_ref


async def submission_loop(runtime: ThreadRuntime) -> None:
    """每 Thread 独立的串行状态提交器。

    处理两类输入：
    - Submission（StartTurn / InterruptTurn / GetForkSnapshot / ShutdownThread）
    - RuntimeSignal（RunnerSucceeded / RunnerFailed / RunnerCancelled）
    """
    try:
        while runtime.state not in ("closing", "closed"):
            msg = await runtime._merged.get()

            match msg:
                case Submission(op=StartTurn(turn_id=tid, request_ref=req_ref)):
                    try:
                        turn = await runtime.accept_turn(
                            StartTurn(turn_id=tid, request_ref=req_ref)
                        )
                        runtime.spawn_turn(turn)
                        if not msg.reply.done():
                            msg.reply.set_result(turn)
                    except RuntimeError as e:
                        # Thread 已有活跃 Turn → 拒绝并发启动
                        if not msg.reply.done():
                            msg.reply.set_exception(e)

                case Submission(op=InterruptTurn(turn_id=tid, reason=reason)):
                    if (
                        runtime.active_turn is None
                        or runtime.active_turn.turn_id != tid
                    ):
                        if not msg.reply.done():
                            msg.reply.set_exception(
                                RuntimeError(f"no active turn {tid}")
                            )
                        continue
                    runner_task = runtime.active_turn.runner_task
                    await runtime.commit_interrupted(tid, reason)
                    if runner_task is not None:
                        runner_task.cancel()
                        try:
                            await runner_task
                        except asyncio.CancelledError:
                            # runner 已被 cancel 并等待完毕 → 预期行为
                            pass
                    if not msg.reply.done():
                        msg.reply.set_result(None)

                case Submission(op=GetForkSnapshot(after_turn_id=after)):
                    try:
                        ctx = runtime.completed_snapshot(after)
                    except KeyError as e:
                        # after_turn_id 指向的 Turn 不存在于已完成记录中
                        if not msg.reply.done():
                            msg.reply.set_exception(e)
                        continue
                    if not msg.reply.done():
                        msg.reply.set_result(ctx)

                case Submission(op=ShutdownThread(reason=reason)):
                    runner_task = None
                    if runtime.active_turn is not None:
                        tid = runtime.active_turn.turn_id
                        runner_task = runtime.active_turn.runner_task
                        await runtime.commit_interrupted(tid, reason)
                    if runner_task is not None:
                        runner_task.cancel()
                        try:
                            await asyncio.shield(runner_task)
                        except asyncio.CancelledError:
                            # 关闭中断后等待被 cancel 的 runner → 预期行为
                            pass
                    if not msg.reply.done():
                        msg.reply.set_result(None)
                    return

                # Runner 完成信号 — 由 _run_turn 发送到 control_queue
                case RunnerSucceeded(turn_id=tid, result_ref=rr, next_context_ref=ncr):
                    await runtime.commit_completed(tid, rr, ncr)

                case RunnerFailed(turn_id=tid, exception_type=et):
                    await runtime.commit_failed(tid, et)

                case RunnerCancelled(turn_id=tid):
                    await runtime.commit_interrupted(tid, "runner_cancelled")
    finally:
        await runtime.ensure_runner_stopped()
        runtime.fail_pending_submissions(ClosedError("thread runtime closed"))


async def _run_turn(runtime: ThreadRuntime, turn: AthenaTurn) -> None:
    """执行 runner 并编排 Memory。

    流程：
    1. Turn 前：maybe_compact() 压缩超限上下文
    2. Turn 中：Runner 执行（LLM 交互 + 工具调用）
    3. Turn 后：record_items() 持久化本轮新增消息到 JSONL
    4. 失败时：rollback(before_index) 丢弃半成品消息

    Runner 签名检测：
    - 有 run_with_context → 五参数 (thread, turn, emit, memory, cancel)
    - 无 run_with_context → 三参数 (thread, turn, emit)
    这保证了与 agent_runner() 包装器的向后兼容。
    """

    async def emit(kind: str, event_ref: ArtifactRef, data: dict | None = None) -> None:
        async with runtime.journal.condition:
            if runtime.active_turn is None:
                raise RuntimeError("no active turn")
            if runtime.active_turn.turn_id != turn.turn_id:
                raise RuntimeError("turn no longer active")
            runtime.journal.append(
                Event(
                    thread_id=runtime.thread_id,
                    turn_id=turn.turn_id,
                    sequence=runtime.journal.next_sequence(),
                    kind=kind,
                    event_ref=event_ref,
                    data=data,
                )
            )
        await runtime._execution_observer.agent_event(
            turn.turn_id, kind, event_ref, data
        )

    before_index: int | None = None

    try:
        # 1. Turn 前：压缩检查 + 快照当前位置
        await runtime.maybe_compact()
        if runtime._ctx is not None:
            before_index = runtime._ctx.snapshot()[0]

        thread = AthenaThread(
            thread_id=runtime.thread_id,
            session_id=runtime.session_id,
            status="running",
            context_ref=runtime.context_ref,
        )

        # 2. 执行 Runner（检测两种签名）
        contextual_runner = getattr(runtime._runner, "run_with_context", None)
        if contextual_runner is not None:
            cancel = (
                runtime.active_turn.cancel_requested
                if runtime.active_turn is not None
                else asyncio.Event()
            )
            outcome = await contextual_runner(thread, turn, emit, runtime._ctx, cancel)
        else:
            outcome = await runtime._runner(thread, turn, emit)

        # 解包结果：支持 AgentOutcome 和 tuple 两种格式
        if hasattr(outcome, "result_ref") and hasattr(outcome, "next_context_ref"):
            result_ref = outcome.result_ref
            next_context_ref = outcome.next_context_ref
        else:
            result_ref, next_context_ref = outcome

        # 3. Turn 后：持久化本轮新增消息
        if runtime._ctx is not None and before_index is not None:
            new_items = runtime._ctx.items_since(before_index)
            runtime.record_items(new_items)

        await runtime.control_queue.put(
            RunnerSucceeded(
                turn_id=turn.turn_id,
                result_ref=result_ref,
                next_context_ref=next_context_ref,
            )
        )

    except asyncio.CancelledError:
        # 取消：仍持久化已产生消息（不完全回滚）
        if runtime._ctx is not None and before_index is not None:
            new_items = runtime._ctx.items_since(before_index)
            runtime.record_items(new_items)
        await runtime.control_queue.put(RunnerCancelled(turn_id=turn.turn_id))
        raise
    except Exception as exc:
        # 4. 失败：回滚半成品消息
        if runtime._ctx is not None and before_index is not None:
            runtime._ctx.rollback(before_index)
        await runtime.control_queue.put(
            RunnerFailed(turn_id=turn.turn_id, exception_type=type(exc).__name__)
        )

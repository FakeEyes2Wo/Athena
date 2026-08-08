"""AgentRuntime — Thread 模型的 Agent 门面(方案 A:门面协调层)。

把 kernel 的 Agent 树语义重新表达在 app_server 的 Thread 模型上:每个逻辑 Agent =
一条 ThreadRuntime(agent_id == thread_id)。公共契约(AgentControl/AgentHandle/AgentRun/
types)由 core.agent 包统一导出,调用方(ProjectRuntime/编排工具)接口不变。
mailbox 与 WaitRegistry 为门面持有(内存);对话经 rollout JSONL 确定性持久化。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from athena.core.agent.models import AgentOutcome
from athena.core.agent.session import RunSession

from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import (
    TERMINAL_RUN_STATUSES,
    AgentBusyError,
    AgentCommandError,
    AgentEvent,
    AgentId,
    AgentMessage,
    AgentPath,
    AgentSnapshot,
    AgentStatus,
    AgentWaitResult,
    ArtifactRef,
    ErrorCode,
    ReturnWhen,
    RunId,
    RunStatus,
    RunSummary,
)
from athena.core.thread_models import AthenaThread
from athena.memory.context_manager import ContextManager

# app_server 符号仅在运行时按需导入:agent_runtime 经 athena.core.agent 包 __init__
# 导出,若在此模块级导入 app_server 会在 app_server 自身导入链(athena.core.*)中
# 构成循环(events → core → agent → agent_runtime → thread_manager → events)。
if TYPE_CHECKING:
    from athena.app_server.exceptions import ClosedError
    from athena.app_server.submissions import TurnTerminalState
    from athena.app_server.thread_manager import RuntimeThreadManager

logger = logging.getLogger(__name__)


@dataclass
class _FacadeRecord:
    agent_id: AgentId
    agent_type: str
    spec: Any  # AgentSpec
    parent_id: AgentId | None
    name: str
    path: AgentPath
    mailbox: deque = field(default_factory=deque)


class _ThreadRunner:
    """把 kernel AgentRunner 协议(经 RunSession 视图)接到 thread run_with_context。

    COMPAT: BaseAgentRunner 主体不动,只在此适配;空唤醒({} 请求)不生成假 trigger
    由 BaseAgentRunner 现有逻辑处理。清理条件: AgentRunner 协议统一为线程 runner 后。
    """

    def __init__(self, runtime: "AgentRuntime") -> None:
        self._runtime = runtime

    async def run(self, thread: AthenaThread, turn, emit) -> AgentOutcome:
        return await self.run_with_context(thread, turn, emit, None, asyncio.Event())

    async def run_with_context(
        self, thread, turn, emit, memory, cancel
    ) -> AgentOutcome:
        if cancel.is_set():
            raise asyncio.CancelledError
        record = self._runtime._records.get(thread.thread_id)
        if record is None:
            raise RuntimeError(f"unknown agent thread: {thread.thread_id}")
        request = record.spec.codec.decode_request(turn.request_ref)
        session = RunSession(
            agent_id=record.agent_id,
            kernel=self._runtime,
            context_ref=thread.context_ref,
            memory=memory,
            mailbox=record.mailbox,
        )
        raw = await record.spec.runner.run(request, session=session, emit=emit)
        response_ref = record.spec.codec.encode_response(raw)
        return AgentOutcome(
            result_ref=response_ref, next_context_ref=thread.context_ref
        )


def _terminal_to_run_status(terminal: TurnTerminalState) -> RunStatus:
    if terminal.cancelled:
        return RunStatus.INTERRUPTED
    if terminal.exception_type is not None:
        return RunStatus.FAILED
    return RunStatus.COMPLETED


class AgentRuntime:
    """Agent 树语义的 Thread 门面;方法签名对齐退役前的 AgentKernel。"""

    def __init__(
        self,
        *,
        type_registry: AgentTypeRegistry,
        project_root: Path | None = None,
        rollout_dir: Path | None = None,
    ) -> None:
        self._registry = type_registry
        root = Path(project_root) if project_root is not None else Path.cwd()
        from athena.app_server.thread_manager import RuntimeThreadManager

        self._manager = RuntimeThreadManager(
            _ThreadRunner(self),
            project_root=root,
            rollout_dir=rollout_dir,
            ctx=ContextManager(),  # 每线程默认空记忆;rollout_dir 存在时由 _make_runtime 恢复覆盖
            on_turn_terminal=self._on_turn_terminal,
        )
        self._records: dict[AgentId, _FacadeRecord] = {}
        self._run_agent: dict[RunId, AgentId] = {}
        self._run_summaries: dict[RunId, RunSummary] = {}
        self._active_turn: dict[AgentId, RunId] = {}
        self._last_terminal: dict[AgentId, RunStatus] = {}
        self._human_waits: dict[str, AgentId] = {}
        self._agent_waits: dict[AgentId, list[AgentId]] = {}
        self._closed_agents: set[AgentId] = set()
        self._paused = False
        self._closed = False

    # ---- 生命周期 ----

    def start(self) -> None:
        """兼容入口;Thread 模型无需预热。"""
        if self._closed:
            raise AgentCommandError(ErrorCode.CLOSED, "runtime is closed")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._manager.aclose("runtime close")

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def has_agent(self, agent_id: AgentId) -> bool:
        return agent_id in self._records

    def _check_open(self) -> None:
        if self._closed:
            raise AgentCommandError(ErrorCode.CLOSED, "runtime is closed")
        if self._paused:
            # COMPAT: kernel 的 pause 曾排队派发;无全局队列下退化为直接报错。
            raise AgentCommandError(ErrorCode.CLOSED, "runtime paused")

    # ---- 创建 / 消息 / 续跑 ----

    async def create_root(self, agent_type: str, task: object, *, name: str = "root"):
        return await self._spawn_agent(None, agent_type, task, name=name)

    async def spawn(
        self,
        parent_id: AgentId,
        agent_type: str,
        task: object,
        *,
        name: str | None = None,
    ):
        if parent_id not in self._records:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown parent: {parent_id}")
        return await self._spawn_agent(parent_id, agent_type, task, name=name)

    async def _spawn_agent(self, parent_id, agent_type, task, *, name):
        self._check_open()
        if not self._registry.contains(agent_type):
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, f"unknown agent_type: {agent_type}"
            )
        agent_id = uuid4().hex
        spec = self._registry.require_spec(agent_type, agent_id=agent_id)
        req_ref = spec.codec.encode_request(task)
        await self._manager.start(agent_id, req_ref, thread_id=agent_id)
        parent = self._records.get(parent_id)
        path = parent.path + (name or agent_type,) if parent else (name or agent_type,)
        self._records[agent_id] = _FacadeRecord(
            agent_id=agent_id,
            agent_type=agent_type,
            spec=spec,
            parent_id=parent_id,
            name=name or agent_type,
            path=path,
        )
        run_id = await self._start_run(agent_id, req_ref)
        return agent_id, run_id

    async def resume_agent(
        self, agent_id: AgentId, *, agent_type: str, name: str | None = None
    ) -> None:
        """重开会话:经确定性 rollout 自动恢复记忆,不触发 turn。

        COMPAT: context_ref 用 agent_id 占位(不持久化旧 context_ref)。清理条件:
        会话恢复把 context_ref 一并持久化后。
        """
        if agent_id in self._records:
            return
        spec = self._registry.require_spec(agent_type, agent_id=agent_id)
        await self._manager.start(agent_id, agent_id, thread_id=agent_id)
        self._records[agent_id] = _FacadeRecord(
            agent_id=agent_id,
            agent_type=agent_type,
            spec=spec,
            parent_id=None,
            name=name or agent_type,
            path=(name or agent_type,),
        )

    async def followup(self, agent_id: AgentId, task: object) -> RunId:
        self._check_open()
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        req_ref = record.spec.codec.encode_request(task)
        return await self._start_run(agent_id, req_ref)

    async def _start_run(self, agent_id: AgentId, req_ref: ArtifactRef) -> RunId:
        from athena.app_server.exceptions import ClosedError

        self._check_open()
        try:
            turn = await self._manager.submit(agent_id, req_ref)
        except ClosedError as exc:
            raise AgentCommandError(ErrorCode.CLOSED, str(exc)) from exc
        except RuntimeError as exc:
            if "active turn" in str(exc):
                raise AgentBusyError() from exc
            raise
        run_id = turn.turn_id
        self._run_agent[run_id] = agent_id
        self._active_turn[agent_id] = run_id
        return run_id

    async def send_message(
        self,
        agent_id: AgentId,
        content: str,
        context_refs: list[ArtifactRef] | None = None,
        *,
        source: AgentId | None = None,
    ) -> None:
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        record.mailbox.append(
            AgentMessage(
                source=source, content=content, context_refs=context_refs or []
            )
        )

    # ---- 等待 ----

    async def wait_run(
        self, run_id: RunId, *, timeout: float | None = None
    ) -> RunSummary:
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            raise KeyError(f"unknown run: {run_id}")
        try:
            result_ref = await asyncio.wait_for(
                self._manager.wait_turn(agent_id, run_id), timeout
            )
        except asyncio.CancelledError:
            # 区分 turn 终态中断与调用方取消:让终态回调先落地再判断,真实取消则传播。
            await asyncio.sleep(0)
            cached = self._run_summaries.get(run_id)
            if cached is not None and cached.status == RunStatus.INTERRUPTED:
                return cached
            raise
        except RuntimeError as exc:
            return RunSummary(
                run_id=run_id,
                agent_id=agent_id,
                status=RunStatus.FAILED,
                error=str(exc),
            )
        summary = RunSummary(
            run_id=run_id,
            agent_id=agent_id,
            status=RunStatus.COMPLETED,
            response_ref=result_ref,
        )
        self._run_summaries[run_id] = summary
        return summary

    async def wait_agent(
        self,
        targets: Collection[AgentId],
        *,
        return_when: ReturnWhen = ReturnWhen.FIRST_COMPLETED,
        timeout: float | None = None,
    ) -> AgentWaitResult:
        """等待一组 Agent 当前 run 终结(每线程串行下即等待各自活跃 turn)。

        COMPAT: 原 kernel 的 park/lease 语义不再存在,退化为普通并发等待。清理条件:
        等待语义内建到 thread 后。
        """
        target_ids = list(targets)

        async def _wait_one(agent_id: AgentId):
            run_id = self._active_turn.get(agent_id)
            if run_id is None:
                return self._last_terminal_summary(agent_id)
            return await self.wait_run(run_id)

        tasks = {t: asyncio.ensure_future(_wait_one(t)) for t in target_ids}
        if return_when == ReturnWhen.ALL_COMPLETED:
            done, pending = await asyncio.wait(list(tasks.values()), timeout=timeout)
        else:
            done, pending = await asyncio.wait(
                list(tasks.values()),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        for t in pending:
            t.cancel()
        completed: dict[AgentId, RunSummary] = {}
        for agent_id, task in tasks.items():
            if task in done and not task.cancelled():
                completed[agent_id] = task.result()
        return AgentWaitResult(completed=completed, timed_out=bool(pending))

    def _last_terminal_summary(self, agent_id: AgentId) -> RunSummary:
        for run_id in reversed(list(self._run_summaries.keys())):
            summary = self._run_summaries[run_id]
            if summary.agent_id == agent_id:
                return summary
        raise AgentCommandError(ErrorCode.NOT_FOUND, f"no run for agent: {agent_id}")

    def run_summary(self, run_id: RunId) -> RunSummary | None:
        cached = self._run_summaries.get(run_id)
        if cached is not None:
            return cached
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            return None
        status = (
            RunStatus.RUNNING
            if self._active_turn.get(agent_id) == run_id
            else RunStatus.QUEUED
        )
        return RunSummary(run_id=run_id, agent_id=agent_id, status=status)

    # ---- 中断 / 关闭 ----

    async def interrupt(self, agent_id: AgentId, reason: str) -> None:
        run_id = self._active_turn.get(agent_id)
        if run_id is None:
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, f"no active run for {agent_id}"
            )
        await self._manager.interrupt(agent_id, run_id, reason)

    async def cancel_run(
        self, run_id: RunId, *, reason: str = "caller_cancelled"
    ) -> None:
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            raise KeyError(f"unknown run: {run_id}")
        await self._manager.interrupt(agent_id, run_id, reason)

    # ---- 等待注册 / 唤醒 ----

    async def wait_for(self, agent_id: AgentId, target_ids: list[AgentId]) -> None:
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        for t in target_ids:
            if t not in self._records:
                raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown target: {t}")
        if all(self._is_terminal(t) for t in target_ids):
            return  # 目标已终态 → 当前 turn 由 runner 干净结束
        self._agent_waits[agent_id] = list(target_ids)

    async def wait_for_human(
        self,
        agent_id: AgentId,
        content: str,
        context_refs: list[ArtifactRef] | None = None,
    ) -> str:
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        del content, context_refs  # COMPAT: 内容待后续人工确认持久化
        request_id = uuid4().hex
        self._human_waits[request_id] = agent_id
        return request_id

    async def human_reply(self, request_id: str, reply: str) -> RunId:
        agent_id = self._human_waits.pop(request_id, None)
        if agent_id is None:
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, f"unknown wait request: {request_id}"
            )
        record = self._records[agent_id]
        record.mailbox.append(
            AgentMessage(source="user", content=reply, context_refs=[])
        )
        req_ref = record.spec.codec.encode_request({})  # 空唤醒:无假 trigger
        return await self._start_run_after_settle(agent_id, req_ref)

    def _is_terminal(self, agent_id: AgentId) -> bool:
        if self.agent_status(agent_id) == AgentStatus.CLOSED:
            return True
        return self._last_terminal.get(agent_id) in TERMINAL_RUN_STATUSES

    async def _resolve_agent_waits(self) -> None:
        for agent_id, target_ids in list(self._agent_waits.items()):
            if all(self._is_terminal(t) for t in target_ids):
                del self._agent_waits[agent_id]
                try:
                    await self._wake(agent_id)
                except Exception as exc:  # noqa: BLE001 — 唤醒失败仅记日志,不阻断解析
                    logger.warning("wake %s failed: %s", agent_id, type(exc).__name__)

    async def _wake(self, agent_id: AgentId) -> None:
        record = self._records.get(agent_id)
        if record is None:
            return
        req_ref = record.spec.codec.encode_request({})  # COMPAT: 空唤醒,无假 trigger
        try:
            await self._start_run_after_settle(agent_id, req_ref)
        except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError) as exc:
            logger.warning("wake %s failed: %s", agent_id, type(exc).__name__)

    async def _start_run_after_settle(
        self, agent_id: AgentId, req_ref: ArtifactRef, *, settle_timeout: float = 30.0
    ) -> RunId:
        """等待当前 turn 终态后再启动新 run,避免与活跃 turn 冲突。

        超时则抛 TimeoutError:调用方(human_reply)将其视为回复失败,而非无限阻塞。
        """
        active = self._active_turn.get(agent_id)
        if active is not None:
            await asyncio.wait_for(self.wait_run(active), settle_timeout)
        return await self._start_run(agent_id, req_ref)

    def _descendants(self, agent_id: AgentId) -> list[AgentId]:
        out: list[AgentId] = []
        for aid, rec in self._records.items():
            if rec.parent_id == agent_id:
                out.append(aid)
                out.extend(self._descendants(aid))
        return out

    async def close(self, agent_id: AgentId, *, recursive: bool = False) -> None:
        if agent_id not in self._records:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        if not recursive:
            live = [
                c
                for c in self._descendants(agent_id)
                if self.agent_status(c) != AgentStatus.CLOSED
            ]
            if live:
                raise AgentCommandError(ErrorCode.BUSY, "has live descendants")
        order = [agent_id] + (self._descendants(agent_id) if recursive else [])
        for aid in order:
            handle = await self._manager.get(aid)
            await handle.shutdown_and_wait("agent close")
        self._closed_agents.update(order)

    # ---- 查询 / 投影 ----

    def list_agents(self, path_prefix: AgentPath | None = None) -> list[AgentSnapshot]:
        snaps = []
        for agent_id, rec in self._records.items():
            if path_prefix is not None and rec.path[: len(path_prefix)] != path_prefix:
                continue
            snaps.append(self._snapshot(rec))
        return sorted(snaps, key=lambda s: s.path)

    def _snapshot(self, rec: _FacadeRecord) -> AgentSnapshot:
        return AgentSnapshot(
            agent_id=rec.agent_id,
            path=rec.path,
            name=rec.name,
            agent_type=rec.agent_type,
            status=self.agent_status(rec.agent_id),
            parent_id=rec.parent_id,
            pending_run_id=self._active_turn.get(rec.agent_id),
        )

    def agent_status(self, agent_id: AgentId) -> AgentStatus | None:
        """同步状态投影(Thread 状态全由门面持有,无需 await Thread 快照)。"""
        if any(a == agent_id for a in self._human_waits.values()):
            return AgentStatus.WAITING_FOR_HUMAN
        if agent_id in self._agent_waits:
            return AgentStatus.WAITING
        if agent_id not in self._records:
            return AgentStatus.CLOSED
        if self._active_turn.get(agent_id) is not None:
            return AgentStatus.RUNNING
        if agent_id in self._closed_agents:
            return AgentStatus.CLOSED
        if self._last_terminal.get(agent_id) == RunStatus.FAILED:
            return AgentStatus.ERROR
        return AgentStatus.IDLE

    def agent_snapshot(self, agent_id: AgentId) -> AgentSnapshot | None:
        rec = self._records.get(agent_id)
        return self._snapshot(rec) if rec is not None else None

    def agent_path(self, agent_id: AgentId) -> AgentPath | None:
        rec = self._records.get(agent_id)
        return rec.path if rec is not None else None

    def registry_spec(self, agent_id: AgentId):
        rec = self._records.get(agent_id)
        if rec is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return rec.spec

    # ---- 事件 ----

    def run_events(
        self, run_id: RunId, after_sequence: int = 0
    ) -> AsyncIterator[AgentEvent]:
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            raise KeyError(f"unknown run: {run_id}")
        return self._session_events(agent_id, run_id, after_sequence)

    def session_events(
        self, agent_id: AgentId, after_sequence: int = 0
    ) -> AsyncIterator[AgentEvent]:
        return self._session_events(agent_id, None, after_sequence)

    def _session_events(
        self, agent_id: AgentId, run_id: RunId | None, after_sequence: int
    ):
        """COMPAT: EventJournal(Event) → AgentEvent(run_id/sequence/kind/event_ref/data)。"""

        async def _gen():
            handle = await self._manager.get(agent_id)
            async for ev in handle.events(after_sequence=after_sequence):
                if run_id is not None and ev.turn_id != run_id:
                    continue
                yield AgentEvent(
                    run_id=ev.turn_id,
                    sequence=ev.sequence,
                    kind=ev.kind,
                    event_ref=ev.event_ref,
                    data=ev.data,
                )

        return _gen()

    # ---- 终态回调(供 ThreadManager 透传) ----

    def _on_turn_terminal(self, turn_id: str, terminal: TurnTerminalState) -> None:
        """同步、无 await;更新状态并调度 wait 解析。"""
        agent_id = self._run_agent.get(turn_id)
        if agent_id is None:
            return
        self._active_turn.pop(agent_id, None)
        status = _terminal_to_run_status(terminal)
        self._last_terminal[agent_id] = status
        self._run_summaries[turn_id] = RunSummary(
            run_id=turn_id,
            agent_id=agent_id,
            status=status,
            response_ref=terminal.result_ref,
            error=terminal.exception_type,
            reason="run interrupted" if terminal.cancelled else None,
        )
        if any(agent_id in targets for targets in self._agent_waits.values()):
            asyncio.create_task(self._resolve_agent_waits())

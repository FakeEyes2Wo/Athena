"""多 Agent 管理 — AgentControl 作为 ThreadRuntime/mailbox 的轻量门面。"""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.core.agent.agent import AgentContext, AgentOutcome
from athena.core.schemas import AthenaThread, AthenaTurn
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from athena.core.agent.agent import Agent


@dataclass(slots=True)
class AgentResult:
    agent_id: str
    result_ref: str = ""
    status: str = "completed"


@dataclass(slots=True, frozen=True)
class AgentEvent:
    kind: str
    event_ref: str
    data: dict[str, Any] | None = None


@dataclass(slots=True)
class AgentHandle:
    """子 Agent 句柄 — future + 事件队列 + 内存引用。"""

    agent_id: str
    _future: asyncio.Future[AgentResult] = field(default_factory=asyncio.Future)
    _task: asyncio.Task[None] | None = None
    _memory: "ContextManager | None" = None
    _events: asyncio.Queue[AgentEvent] = field(default_factory=asyncio.Queue)

    async def wait(self, *, timeout: float | None = None) -> AgentResult:
        """等待子 Agent 完成。shield 防止父任务取消传播到子 Agent future。"""
        return await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout)

    async def next_event(self, *, timeout: float | None = None) -> AgentEvent:
        return await asyncio.wait_for(self._events.get(), timeout=timeout)

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


class AgentControl:
    """子 Agent 管理器 — Semaphore 限制并发，独立 ContextManager 隔离内存。"""

    def __init__(self, *, max_concurrency: int = 8) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._handles: dict[str, AgentHandle] = {}
        self._counter = 0

    async def spawn(self, agent: "Agent", task: str, *, emit=None) -> AgentHandle:
        """创建子 Agent 并启动后台执行。Semaphore 控制并发数。"""
        await self._semaphore.acquire()
        self._counter += 1
        agent_id = f"sub-{self._counter}"
        handle = AgentHandle(agent_id=agent_id, _memory=ContextManager())
        self._handles[agent_id] = handle
        handle._task = asyncio.create_task(
            self._run(agent_id, agent, task, handle, emit)
        )
        return handle

    async def _run(
        self, agent_id: str, agent: "Agent", task: str, handle: AgentHandle, emit
    ) -> None:
        """子 Agent 执行体：构造合成 Thread/Turn → agent.run() → 解析 future。

        Semaphore 在 spawn 时获取，在 finally 中释放，保证最大并发数。
        """
        try:
            memory = handle._memory
            assert memory is not None

            async def _emit(
                kind: str, event_ref: str, data: dict[str, Any] | None = None
            ) -> None:
                # 双通道：写入 handle 事件队列 + 转发到父级 emit
                await handle._events.put(AgentEvent(kind, event_ref, data))
                if emit is not None:
                    await emit(kind, event_ref, data)

            thread = AthenaThread(
                thread_id=agent_id,
                session_id="sub",
                status="running",
                context_ref=f"sub://{agent_id}",
            )
            turn = AthenaTurn(
                turn_id=f"{agent_id}-1",
                thread_id=agent_id,
                request_ref=task or "task",
                status="running",
            )
            ctx = AgentContext(
                thread, turn, _emit, agent.config.tools, asyncio.Event(), memory
            )

            outcome: AgentOutcome = await agent.run(ctx)
            handle._future.set_result(
                AgentResult(agent_id=agent_id, result_ref=outcome.result_ref)
            )
        except asyncio.CancelledError:
            # AgentHandle.cancel() 或外部中断 → 标记为 cancelled 而非异常
            if not handle._future.done():
                handle._future.set_result(
                    AgentResult(agent_id=agent_id, status="cancelled")
                )
        except Exception as exc:
            if not handle._future.done():
                handle._future.set_exception(exc)
        finally:
            self._semaphore.release()
            self._handles.pop(agent_id, None)

    async def send_message(self, agent_id: str, message: str) -> None:
        """向运行中的子 Agent 发送消息，追加到其 ContextManager。"""
        handle = self._handles.get(agent_id)
        if handle is None or handle._memory is None:
            raise KeyError(f"unknown agent: {agent_id}")
        handle._memory.append(ModelRequest(parts=[UserPromptPart(content=message)]))

    async def interrupt(self, agent_id: str) -> None:
        h = self._handles.get(agent_id)
        if h:
            h.cancel()

    def list_agents(self) -> list[str]:
        return list(self._handles.keys())

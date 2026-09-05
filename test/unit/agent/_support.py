import asyncio
import json
from pathlib import Path

from athena.agents.base_runner import BaseAgentRunner
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent

from athena.core.agent.types import JsonCodec
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec
from athena.core.artifact_store import LocalArtifactStore


def request_payload(payload: dict) -> dict:
    """把业务 payload 包成触发消息 ``{"content": <json>}``。"""
    return {"content": json.dumps(payload, ensure_ascii=False)}


async def eventually(pred, timeout: float = 3) -> bool:
    """轮询直到谓词为真；超时返回 False。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0)
    return True


class EchoRunner:
    """回显请求；可选等待。兼容 AgentRuntime 的 AgentRunner 协议。"""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.block: asyncio.Event | None = None

    async def run(self, request: object, *, session, emit) -> dict:
        self.calls.append(request)
        await emit("agent/step", "event://step", {"n": len(self.calls)})
        if self.block is not None:
            await self.block.wait()
        return {"echo": request}


class BlockingRunner:
    """启动后阻塞直到 release，用于中断/并发测试。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def run(self, request: object, *, session, emit) -> dict:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            # 外部中断（interrupt/close）→ 记录标志后原样传播
            self.cancelled = True
            raise
        return {"done": True}


async def collect_events(source, n):
    """从订阅式事件流收集恰好 n 条后终止（事件流为订阅式，不会自然结束）。"""
    events = []
    async for event in source:
        events.append(event)
        if len(events) >= n:
            break
    return events


_TERMINAL_EVENT_KINDS = ("turn_completed", "turn_failed", "turn_interrupted")


async def collect_until_terminal(source):
    """收集到首个终态事件（turn_completed/failed/interrupted）为止。

    Thread 每 turn 会额外产出 ``execution/health`` 事件，终态事件之前的事件
    数量不固定，因此按终态种类而非计数收集。
    """
    events = []
    async for event in source:
        events.append(event)
        if event.kind in _TERMINAL_EVENT_KINDS:
            break
    return events


class StubAgent(BaseAgent):
    """把 input_text 写为 Artifact 的确定性 agent。"""

    def __init__(self, store):
        self._store = store

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        ref = await self._store.put_text(json.dumps({"result": ctx.input_text}))
        return AgentOutcome(result_ref=ref)


class BlockingAgent(BaseAgent):
    """置位 gate 后阻塞,等待被中断。"""

    def __init__(self, gate: asyncio.Event):
        self._gate = gate

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self._gate.set()
        await asyncio.sleep(30)
        return AgentOutcome(result_ref="art:never")


def make_runtime(tmp_path: Path) -> AgentRuntime:
    """构造只注册 ``stub`` 类型的门面。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    registry.register(
        "stub",
        lambda aid: AgentSpec(
            runner=BaseAgentRunner(StubAgent(store)), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / "sessions",
    )
    rt.start()
    return rt

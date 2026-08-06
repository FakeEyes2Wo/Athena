"""测试共享的 codec 与 runner 替身。"""

import asyncio
import json


class JsonCodec:
    """请求/响应以 JSON 字符串作为 ArtifactRef。"""

    def encode_request(self, value: object) -> str:
        return json.dumps(value)

    def decode_request(self, ref: str) -> object:
        return json.loads(ref)

    def encode_response(self, value: object) -> str:
        return json.dumps(value)

    def decode_response(self, ref: str) -> object:
        return json.loads(ref)


class EchoRunner:
    """回显请求；可选等待。"""

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


_TERMINAL_EVENT_KINDS = ("run_completed", "run_failed", "run_interrupted")


async def collect_until_terminal(source):
    """收集到首个终态事件（run_completed/failed/interrupted）为止。"""
    events = []
    async for event in source:
        events.append(event)
        if event.kind in _TERMINAL_EVENT_KINDS:
            break
    return events

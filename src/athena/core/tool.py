"""BaseTool, ToolRegistry, and ``@tool`` decorator."""

import asyncio
import json
import time
import traceback
from abc import ABC, abstractmethod
from typing import Any, Callable

from athena.core.tool_types import (
    TOOL_BEGIN,
    TOOL_END,
    TOOL_ERROR,
    TOOL_PREVIEW_CHARS,
    EmitEvent,
    ToolContext,
    ToolResult,
    ToolSpec,
)


def _preview(result: ToolResult) -> str:
    """结果的短文本预览，仅供 UI 展示；非字符串数据走 JSON 序列化。"""
    data = result.data
    if data is None:
        return ""
    if isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, ensure_ascii=False, default=str)
    return text[:TOOL_PREVIEW_CHARS]


async def _noop_emit(_k: str, _r: str, _d: dict | None = None) -> None:
    pass


def _sync_ctx() -> ToolContext:
    return ToolContext("", "sync", _noop_emit, asyncio.Event())


# ── BaseTool ──


class BaseTool(ABC):
    """Subclass: ``spec`` + ``execute()`` (returns raw data, not ToolResult).

    Call:  ``tool.invoke(**kw)`` (sync)  /  ``tool.ainvoke(ctx, **kw)`` (async)
    """

    spec: ToolSpec

    @abstractmethod
    async def execute(self, input: dict, ctx: ToolContext) -> Any: ...

    async def _execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """Wrap return value / exception into ``ToolResult``."""
        try:
            raw = await self.execute(input, ctx)
            return raw if isinstance(raw, ToolResult) else ToolResult(data=raw)
        except asyncio.CancelledError:
            # 工具执行被取消 → 不包装为 ToolResult，直接传播
            raise
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                data={"traceback": traceback.format_exc()},
            )

    def invoke(self, **input: Any) -> ToolResult:
        return asyncio.run(self.ainvoke(_sync_ctx(), **input))

    async def ainvoke(self, ctx: ToolContext, **input: Any) -> ToolResult:
        """Lifecycle: validate → begin → _execute → end/error。

        事件携带足够 UI 渲染的最小载荷：begin 带工具名与参数，
        end 带成功标志、耗时和结果预览。完整结果只走返回值，不进事件流。
        """
        input = self._validate(input)
        started = time.monotonic()
        await ctx.emit(
            TOOL_BEGIN,
            f"ev:{ctx.call_id}:begin",
            {"tool": ctx.tool_name, "call_id": ctx.call_id, "args": input},
        )
        try:
            result = await self._execute(input, ctx)
        except asyncio.CancelledError:
            # 工具调用被外部取消 → 通知错误后重新传播
            await ctx.emit(
                TOOL_ERROR,
                f"ev:{ctx.call_id}:error",
                {
                    "tool": ctx.tool_name,
                    "call_id": ctx.call_id,
                    "ok": False,
                    "error": "cancelled",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            raise
        self._truncate(result)
        await ctx.emit(
            TOOL_END if result.success else TOOL_ERROR,
            f"ev:{ctx.call_id}:end",
            {
                "tool": ctx.tool_name,
                "call_id": ctx.call_id,
                "ok": result.success,
                "error": result.error,
                "preview": _preview(result),
                "truncated": result.truncated,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return result

    def _validate(self, input: dict) -> dict:
        return input

    def _truncate(self, r: ToolResult) -> None:
        if isinstance(r.data, str) and len(r.data) > self.spec.max_result_chars:
            r.truncated = True


# ── @tool decorator ──


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    input_schema: dict | None = None,
    **spec_kwargs: Any,
) -> Callable[[Any], BaseTool]:
    """Decorator: turn an async function into a ``BaseTool`` instance."""

    def deco(fn):
        doc = (fn.__doc__ or "").strip()
        desc = description or (doc.split("\n")[0] if doc else fn.__name__)

        class _T(BaseTool):
            spec = ToolSpec(
                name=name or fn.__name__,
                description=desc,
                input_schema=input_schema or {"type": "object", "properties": {}},
                **spec_kwargs,
            )

            async def execute(self, input: dict, ctx: ToolContext) -> Any:
                return await fn(**input)

        _T.__name__ = fn.__name__
        _T.__qualname__ = fn.__qualname__
        _T.__doc__ = fn.__doc__
        return _T()

    return deco


# ── ToolRegistry ──


class ToolRegistry:
    """Sorted registry — specs ordered by name for prompt-cache stability."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._sorted: list[BaseTool] = []

    def register(self, t: BaseTool) -> None:
        if t.spec.name in self._tools:
            raise KeyError(f"Tool '{t.spec.name}' already registered")
        self._tools[t.spec.name] = t
        self._sorted = sorted(self._tools.values(), key=lambda t: t.spec.name)

    def resolve(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not found")
        return self._tools[name]

    @property
    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._sorted]

    def search(self, q: str) -> list[ToolSpec]:
        q = q.lower()
        return [
            t.spec
            for t in self._sorted
            if q in t.spec.name.lower() or q in t.spec.description.lower()
        ]

    def invoke(self, name: str, **inp: Any) -> ToolResult:
        return self.resolve(name).invoke(**inp)

    def dispatch(
        self, calls: list[tuple[str, dict]]
    ) -> list[ToolResult | BaseException]:
        return asyncio.run(
            self._dispatch(
                [(n, f"sync-{i}", inp) for i, (n, inp) in enumerate(calls)],
                _noop_emit,
                asyncio.Event(),
            )
        )

    async def adispatch(
        self,
        calls: list[tuple[str, str, dict]],
        emit: EmitEvent,
        cancel: asyncio.Event,
    ) -> list[ToolResult | BaseException]:
        return await self._dispatch(calls, emit, cancel)

    async def _dispatch(
        self,
        calls: list[tuple[str, str, dict]],
        emit: EmitEvent,
        cancel: asyncio.Event,
    ) -> list[ToolResult | BaseException]:
        return await asyncio.gather(
            *[
                asyncio.create_task(
                    self.resolve(n).ainvoke(ToolContext(n, cid, emit, cancel), **inp)
                )
                for n, cid, inp in calls
            ],
            return_exceptions=True,
        )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

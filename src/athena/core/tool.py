"""BaseTool、ToolRegistry 和 ``@tool`` 装饰器。"""

import asyncio
import traceback
from abc import ABC, abstractmethod
from typing import Any, Callable

from athena.core.tool_types import (
    TOOL_BEGIN,
    TOOL_END,
    TOOL_ERROR,
    ToolContext,
    ToolResult,
    ToolSpec,
)


async def _noop_emit(_k: str, _r: str, _d: dict | None = None) -> None:
    pass


def _sync_ctx() -> ToolContext:
    return ToolContext("", "sync", _noop_emit, asyncio.Event())


class BaseTool(ABC):
    """子类需定义 ``spec`` + ``execute()``（返回原始数据，非 ToolResult）。

    调用方式：``tool.invoke(**kw)``（同步） / ``tool.ainvoke(ctx, **kw)``（异步）
    """

    spec: ToolSpec

    @abstractmethod
    async def execute(self, input: dict, ctx: ToolContext) -> Any: ...

    async def _execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """将返回值或异常包装为 ``ToolResult``。

        取消（CancelledError 继承 BaseException）不会被 ``except Exception``
        捕获，因此直接向上传播，由调用方处理。
        """
        try:
            raw = await self.execute(input, ctx)
            return raw if isinstance(raw, ToolResult) else ToolResult(data=raw)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                data={"traceback": traceback.format_exc()},
            )

    def invoke(self, **input: Any) -> ToolResult:
        return asyncio.run(self.ainvoke(_sync_ctx(), **input))

    async def ainvoke(self, ctx: ToolContext, **input: Any) -> ToolResult:
        """生命周期：开始 → 执行 → 结束/错误。"""
        await ctx.emit(TOOL_BEGIN, f"ev:{ctx.call_id}:begin", None)
        try:
            result = await self._execute(input, ctx)
        except asyncio.CancelledError:
            # 工具调用被外部取消 → 通知错误后重新传播
            await ctx.emit(TOOL_ERROR, f"ev:{ctx.call_id}:error", None)
            raise
        await ctx.emit(
            TOOL_END if result.success else TOOL_ERROR,
            f"ev:{ctx.call_id}:end",
            None,
        )
        return result


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    input_schema: dict | None = None,
    **spec_kwargs: Any,
) -> Callable[[Any], BaseTool]:
    """装饰器：将异步函数转为 ``BaseTool`` 实例。"""

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


class ToolRegistry:
    """有序注册表 — 按名称排序 spec 以保证 prompt-cache 稳定性。"""

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

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

"""BaseTool、ToolRegistry 和 ``@tool`` 装饰器。"""

import asyncio
import inspect
import traceback
import types
from abc import ABC, abstractmethod
from typing import Any, Union, get_args, get_origin

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
    async def execute(self, input: dict, ctx: ToolContext) -> Any:
        """执行工具逻辑，返回原始数据（非 ToolResult）。"""

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
        """同步入口：用独立事件循环执行一次调用。"""
        return asyncio.run(self.ainvoke(_sync_ctx(), **input))

    async def ainvoke(self, ctx: ToolContext, **input: Any) -> ToolResult:
        """生命周期：开始 → 执行 → 结束/错误。"""
        event_data: dict[str, Any] = {"tool": ctx.tool_name}
        path = input.get("path")
        if isinstance(path, str):
            event_data["path"] = path
        await ctx.emit(TOOL_BEGIN, f"ev:{ctx.call_id}:begin", event_data)
        try:
            result = await self._execute(input, ctx)
        except asyncio.CancelledError:
            # 工具调用被外部取消 → 通知错误后重新传播
            await ctx.emit(TOOL_ERROR, f"ev:{ctx.call_id}:error", None)
            raise
        await ctx.emit(
            TOOL_END if result.success else TOOL_ERROR,
            f"ev:{ctx.call_id}:end",
            event_data,
        )
        return result


class StackTool(BaseTool):
    """只有一个注入依赖的工具底座：把 ``stack`` 存在 ``self.stack`` 上。"""

    def __init__(self, stack: Any) -> None:
        self.stack = stack


def _type_to_schema(annotation: Any) -> dict:
    """Python 类型注解 → JSON Schema 片段（str/int/float/bool/list/dict/Optional）。

    无法识别/未注解 → 空 dict（不约束该参数）。
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return _type_to_schema(args[0]) if args else {}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if origin is list or annotation is list:
        items = get_args(annotation)
        return {"type": "array", "items": _type_to_schema(items[0]) if items else {}}
    if annotation is dict or origin is dict:
        return {"type": "object"}
    return {}


def _schema_from_signature(fn: Any) -> dict:
    """由函数签名生成 JSON Schema：无默认值 → required；非 None 默认值 → default。

    ``*args``/``**kwargs`` 忽略；``X | None = None`` 视为可选且不写 default。
    """
    props: dict[str, dict] = {}
    required: list[str] = []
    for pname, p in inspect.signature(fn).parameters.items():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        prop = _type_to_schema(p.annotation)
        if p.default is not inspect.Parameter.empty:
            if p.default is not None:
                prop["default"] = p.default
        else:
            required.append(pname)
        props[pname] = prop
    return {"type": "object", "properties": props, "required": required}


def tool(
    _fn: Any | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    input_schema: dict | None = None,
    **spec_kwargs: Any,
) -> Any:
    """装饰器：把异步函数转成 ``BaseTool`` 实例。

    支持裸用 ``@tool`` 与带参 ``@tool(name=..., description=..., input_schema=...)``
    两种写法。未显式给出的 name/description/input_schema 分别由函数名、**完整
    docstring**、类型注解自动推导。
    """

    def deco(fn: Any) -> BaseTool:
        """把被装饰函数包装为 BaseTool 实例。"""
        spec_name = name or fn.__name__
        doc = inspect.cleandoc(fn.__doc__ or "")
        spec_desc = description if description is not None else (doc or fn.__name__)
        schema = (
            input_schema if input_schema is not None else _schema_from_signature(fn)
        )

        class _T(BaseTool):
            spec = ToolSpec(
                name=spec_name,
                description=spec_desc,
                input_schema=schema,
                **spec_kwargs,
            )

            async def execute(self, input: dict, ctx: ToolContext) -> Any:
                """透传调用原始函数。"""
                return await fn(**input)

        _T.__name__ = fn.__name__
        _T.__qualname__ = fn.__qualname__
        _T.__doc__ = fn.__doc__
        return _T()

    return deco(_fn) if _fn is not None else deco


class ToolRegistry:
    """有序注册表 — 按名称排序 spec 以保证 prompt-cache 稳定性。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._sorted: list[BaseTool] = []

    def register(self, t: BaseTool) -> None:
        """注册工具；重名报错，按名称维持排序。"""
        if t.spec.name in self._tools:
            raise KeyError(f"Tool '{t.spec.name}' already registered")
        self._tools[t.spec.name] = t
        self._sorted = sorted(self._tools.values(), key=lambda t: t.spec.name)

    def resolve(self, name: str) -> BaseTool:
        """按名称取工具；未注册报 KeyError。"""
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not found")
        return self._tools[name]

    @property
    def specs(self) -> list[ToolSpec]:
        """按名称排序的工具 spec 列表。"""
        return [t.spec for t in self._sorted]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

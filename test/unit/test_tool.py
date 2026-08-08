"""工具抽象层的单元测试。"""

import asyncio

import pytest

from athena.core.tool import BaseTool, ToolRegistry, tool
from athena.core.tool_types import (
    TOOL_BEGIN,
    TOOL_END,
    TOOL_ERROR,
    ToolContext,
    ToolResult,
    ToolSpec,
)


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="echo",
        input_schema={},
    )

    async def execute(self, inpt: dict, ctx: ToolContext) -> dict:
        return inpt  # 原始字典，由 _execute 自动包装


class _FailingTool(BaseTool):
    spec = ToolSpec(name="fail", description="fail", input_schema={})

    async def execute(self, inpt: dict, ctx: ToolContext):
        raise RuntimeError("boom")  # 由 _execute 捕获 → ToolResult(success=False)


class _CancellingTool(BaseTool):
    spec = ToolSpec(name="cancel_me", description="cancel", input_schema={})

    async def execute(self, inpt: dict, ctx: ToolContext):
        raise asyncio.CancelledError()  # 不捕获 → 向上传播


class _ToolResultTool(BaseTool):
    """直接返回 ToolResult 的工具 — _execute 透传。"""

    spec = ToolSpec(name="direct", description="direct", input_schema={})

    async def execute(self, inpt: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(data={"custom": True}, success=True)


@tool(name="deco_echo")
async def _deco_echo(text: str, repeat: int = 1) -> dict:
    """使用 @tool 装饰器的 Echo。"""
    return {"text": text, "repeat": repeat}


@tool(name="deco_fail")
async def _deco_fail() -> dict:
    """始终失败。"""
    raise ValueError("bad input")


def _arun(coro):
    return asyncio.run(coro)


async def _collect_events(tool: BaseTool, **inpt) -> tuple[ToolResult, list[str]]:
    events: list[str] = []

    async def emit(kind: str, _ref: str, _data: dict | None = None) -> None:
        events.append(kind)

    ctx = ToolContext(tool.spec.name, "test-1", emit, asyncio.Event())
    result = await tool.ainvoke(ctx, **inpt)
    return result, events


class TestBaseTool:
    def test_sync_invoke(self):
        result = _EchoTool().invoke(x=1, y="hello")
        assert result.success
        assert result.data == {"x": 1, "y": "hello"}

    def test_ainvoke_events(self):
        result, events = _arun(_collect_events(_EchoTool(), a=1))
        assert result.success
        assert result.data == {"a": 1}
        assert TOOL_BEGIN in events
        assert TOOL_END in events

    def test_ainvoke_error_wrapped(self):
        """异常 → ToolResult(success=False)，触发 TOOL_ERROR。"""
        result, events = _arun(_collect_events(_FailingTool()))
        assert result.success is False
        assert "RuntimeError" in result.error
        assert TOOL_BEGIN in events
        assert TOOL_ERROR in events

    def test_ainvoke_cancelled_error(self):
        """CancelledError 不捕获 — 向上传播。"""
        with pytest.raises(asyncio.CancelledError):
            _arun(_collect_events(_CancellingTool()))

    def test_direct_tool_result_passthrough(self):
        result, events = _arun(_collect_events(_ToolResultTool()))
        assert result.success
        assert result.data == {"custom": True}


class TestToolDecorator:
    def test_decorated_invoke(self):
        t = _deco_echo
        result = t.invoke(text="hi")
        assert result.success
        assert result.data == {"text": "hi", "repeat": 1}

    def test_decorated_ainvoke(self):
        t = _deco_echo
        result, events = _arun(_collect_events(t, text="x", repeat=3))
        assert result.data == {"text": "x", "repeat": 3}
        assert TOOL_BEGIN in events
        assert TOOL_END in events

    def test_decorated_spec(self):
        assert _deco_echo.spec.name == "deco_echo"
        assert _deco_echo.spec.description == "使用 @tool 装饰器的 Echo。"

    def test_decorated_error(self):
        result, events = _arun(_collect_events(_deco_fail))
        assert result.success is False
        assert "ValueError" in result.error
        assert TOOL_ERROR in events


class TestToolRegistry:
    def test_register_and_resolve(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        assert "echo" in reg
        assert len(reg) == 1

    def test_register_duplicate(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        with pytest.raises(KeyError):
            reg.register(_EchoTool())

    def test_resolve_missing(self):
        with pytest.raises(KeyError):
            ToolRegistry().resolve("nope")

    def test_specs_sorted(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        reg.register(_deco_echo)  # "deco_echo"
        assert [s.name for s in reg.specs] == ["deco_echo", "echo"]

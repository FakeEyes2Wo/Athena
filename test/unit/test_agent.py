"""Unit tests for agent abstractions."""

import asyncio
from types import SimpleNamespace

import pytest

from athena.core.agent import (
    Agent,
    AgentConfig,
    AgentContext,
    AgentControl,
    AgentOutcome,
    BaseAgent,
    StreamEvent,
    agent_runner,
    create_agent,
)
from athena.core.schemas import ArtifactRef, AthenaThread, AthenaTurn
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


def _arun(coro):
    return asyncio.run(coro)


# ── minimal agent ──


class _SpyAgent(BaseAgent):
    name = "spy"
    description = "Records calls for testing."

    def __init__(self):
        self.calls: list[AgentContext] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.calls.append(ctx)
        return AgentOutcome(result_ref="result://ok", next_context_ref="context://next")


class _ToolUsingAgent(BaseAgent):
    name = "tool_user"
    description = "Uses tools."

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        r = await self.tool(ctx, "echo", msg="hello")
        return AgentOutcome(
            result_ref=r.data["msg"], next_context_ref=ctx.thread.context_ref
        )


# ── minimal tool ──


class _EchoTool(BaseTool):
    spec = ToolSpec(name="echo", description="echo", input_schema={})

    async def execute(self, inpt: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(data=inpt)


# ── tests ──


class TestBaseAgent:
    def test_create_agent_uses_injected_model_client(self):
        client = object()
        tools = ToolRegistry()

        agent = create_agent(
            model="test-model",
            tools=tools,
            system_prompt="system",
            client=client,
        )

        assert agent.config.model == "test-model"
        assert agent._provider.client is client

    def test_run_receives_context(self):
        agent = _SpyAgent()
        tools = ToolRegistry()
        runner = agent_runner(agent, tools)

        thread = AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        )
        turn = AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="req://1", status="running"
        )

        async def emit(_k, _r, _d=None):
            pass

        outcome = _arun(runner(thread, turn, emit))

        assert outcome.result_ref == "result://ok"
        assert outcome.next_context_ref == "context://next"
        assert len(agent.calls) == 1
        ctx = agent.calls[0]
        assert ctx.thread.thread_id == "t1"
        assert ctx.turn.turn_id == "t1.1"
        assert ctx.tools is tools
        assert isinstance(ctx.cancel, asyncio.Event)

    def test_tool_invoke(self):
        tools = ToolRegistry()
        tools.register(_EchoTool())
        agent = _ToolUsingAgent()
        runner = agent_runner(agent, tools)

        thread = AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        )
        turn = AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="req://1", status="running"
        )

        async def emit(_k, _r, _d=None):
            pass

        outcome = _arun(runner(thread, turn, emit))
        assert outcome.result_ref == "hello"

    async def test_provider_error_is_not_reported_as_success(self):
        class ErrorProvider:
            async def stream(self, *_args):
                yield StreamEvent(kind="error", data={"message": "provider failed"})

        tools = ToolRegistry()
        agent = Agent(AgentConfig("model", "system", tools))
        agent._provider = ErrorProvider()
        ctx = AgentContext(
            AthenaThread(
                thread_id="t1",
                session_id="s1",
                status="running",
                context_ref="ctx://0",
            ),
            AthenaTurn(
                turn_id="t1.1",
                thread_id="t1",
                request_ref="request",
                status="running",
            ),
            lambda *_args: asyncio.sleep(0),
            tools,
            asyncio.Event(),
        )

        with pytest.raises(RuntimeError, match="provider failed"):
            await agent.run(ctx)

    async def test_non_concurrency_safe_tool_is_a_barrier(self):
        timeline: list[str] = []

        class UnsafeTool(BaseTool):
            spec = ToolSpec(
                name="unsafe",
                description="unsafe",
                input_schema={},
                concurrency_safe=False,
            )

            async def execute(self, input: dict, ctx: ToolContext):
                timeline.append("unsafe:start")
                await asyncio.sleep(0)
                timeline.append("unsafe:end")
                return "unsafe"

        class SafeTool(BaseTool):
            spec = ToolSpec(name="safe", description="safe", input_schema={})

            async def execute(self, input: dict, ctx: ToolContext):
                timeline.append("safe:start")
                return "safe"

        class CallsProvider:
            def __init__(self):
                self.calls = 0

            async def stream(self, *_args):
                self.calls += 1
                if self.calls == 1:
                    yield StreamEvent(
                        "function_call",
                        {"call_id": "1", "name": "unsafe", "arguments": {}},
                    )
                    yield StreamEvent(
                        "function_call",
                        {"call_id": "2", "name": "safe", "arguments": {}},
                    )
                else:
                    yield StreamEvent(
                        "text_delta", {"delta": "done", "accumulated": "done"}
                    )
                yield StreamEvent("response_completed")

        tools = ToolRegistry()
        tools.register(UnsafeTool())
        tools.register(SafeTool())
        agent = Agent(AgentConfig("model", "system", tools))
        agent._provider = CallsProvider()
        ctx = AgentContext(
            AthenaThread(
                thread_id="t1",
                session_id="s1",
                status="running",
                context_ref="ctx://0",
            ),
            AthenaTurn(
                turn_id="t1.1",
                thread_id="t1",
                request_ref="request",
                status="running",
            ),
            lambda *_args: asyncio.sleep(0),
            tools,
            asyncio.Event(),
        )

        await agent.run(ctx)

        assert timeline == ["unsafe:start", "unsafe:end", "safe:start"]


class TestAgentControl:
    async def test_message_and_streaming_event_reach_running_subagent(self):
        class StreamingAgent:
            def __init__(self):
                self.config = SimpleNamespace(tools=ToolRegistry())
                self.release = asyncio.Event()
                self.context = None

            async def run(self, ctx: AgentContext) -> AgentOutcome:
                self.context = ctx
                await ctx.emit(
                    "agent/text_delta", "event://delta", {"delta": "working"}
                )
                await self.release.wait()
                return AgentOutcome("result://ok", "context://next")

        agent = StreamingAgent()
        control = AgentControl(max_concurrency=1)
        handle = await control.spawn(agent, "full task text")

        event = await handle.next_event(timeout=0.1)
        await control.send_message(handle.agent_id, "follow up")
        agent.release.set()
        result = await handle.wait(timeout=0.1)

        assert event.kind == "agent/text_delta"
        assert event.data == {"delta": "working"}
        assert agent.context.memory.items[-1].parts[0].content == "follow up"
        assert result.status == "completed"

    def test_chat_completions_tool_schema_is_nested(self):
        spec = ToolSpec(
            name="echo", description="echo", input_schema={"type": "object"}
        )

        assert spec.to_openai_tool() == {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "echo",
                "parameters": {"type": "object"},
            },
        }

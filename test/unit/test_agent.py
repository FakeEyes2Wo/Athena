"""Agent 抽象层的单元测试。"""

import asyncio
from dataclasses import fields
from inspect import signature
import pytest

import athena.core as core_api
import athena.core.agent as agent_api
from athena.core.agent.control import AgentControl, AgentEvent, AgentHandle, AgentResult
from athena.core.agent.models import (
    AgentConfig,
    AgentContext,
    AgentOutcome,
    StepOutcome,
    ToolCall,
)
from athena.core.agent.provider import ResponsesProvider, StreamEvent
from athena.core.agent.runtime import (
    Agent,
    BaseAgent,
    agent_runner,
    create_agent,
    create_code_agent,
)
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.memory.context_manager import ContextManager


def _arun(coro):
    return asyncio.run(coro)


def test_public_agent_exports_point_to_canonical_owners() -> None:
    assert agent_api.Agent is Agent
    assert agent_api.AgentConfig is AgentConfig
    assert agent_api.AgentContext is AgentContext
    assert agent_api.AgentControl is AgentControl
    assert agent_api.AgentEvent is AgentEvent
    assert agent_api.AgentHandle is AgentHandle
    assert agent_api.AgentOutcome is AgentOutcome
    assert agent_api.AgentResult is AgentResult
    assert agent_api.BaseAgent is BaseAgent
    assert agent_api.ResponsesProvider is ResponsesProvider
    assert agent_api.StepOutcome is StepOutcome
    assert agent_api.StreamEvent is StreamEvent
    assert agent_api.ToolCall is ToolCall
    assert agent_api.agent_runner is agent_runner
    assert agent_api.create_agent is create_agent
    assert agent_api.create_code_agent is create_code_agent

    assert core_api.Agent is Agent
    assert core_api.AgentConfig is AgentConfig
    assert core_api.AgentContext is AgentContext
    assert core_api.AgentControl is AgentControl
    assert core_api.AgentEvent is AgentEvent
    assert core_api.AgentOutcome is AgentOutcome
    assert core_api.BaseAgent is BaseAgent
    assert core_api.StreamEvent is StreamEvent
    assert core_api.ToolCall is ToolCall
    assert core_api.agent_runner is agent_runner
    assert core_api.create_agent is create_agent


def test_code_agent_interface_has_four_parameters_and_four_fields() -> None:
    assert list(signature(create_code_agent).parameters) == [
        "model",
        "tools",
        "system_prompt",
        "config",
    ]
    assert [field.name for field in fields(AgentConfig)] == [
        "max_turns",
        "max_tokens",
        "temperature",
        "name",
    ]


def test_model_combines_name_and_client() -> None:
    client = object()
    model = ResponsesProvider("test-model", client=client)
    assert model.model_name == "test-model"
    assert model.client is client


def test_environment_builds_three_independent_core_agents() -> None:
    model = ResponsesProvider("test-model", client=object())
    agents = [
        create_code_agent(
            model,
            ToolRegistry(),
            f"{name} prompt",
            AgentConfig(name=f"{name}-agent"),
        )
        for name in ("code", "data", "plot")
    ]
    assert all(type(agent) is Agent for agent in agents)
    assert [agent.name for agent in agents] == [
        "code-agent",
        "data-agent",
        "plot-agent",
    ]
    assert all(
        set(vars(agent)) == {"model", "tools", "system_prompt", "config"}
        for agent in agents
    )
    assert len({id(agent) for agent in agents}) == 3


class _SpyAgent(BaseAgent):
    name = "spy"
    description = "记录调用以供测试。"

    def __init__(self):
        self.calls: list[AgentContext] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.calls.append(ctx)
        return AgentOutcome(result_ref="result://ok", next_context_ref="context://next")


class _ToolUsingAgent(BaseAgent):
    name = "tool_user"
    description = "使用工具。"

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        r = await self.tool(ctx, "echo", msg="hello")
        return AgentOutcome(
            result_ref=r.data["msg"], next_context_ref=ctx.thread.context_ref
        )


class _EchoTool(BaseTool):
    spec = ToolSpec(name="echo", description="echo", input_schema={})

    async def execute(self, inpt: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(data=inpt)


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

        assert agent.model.model_name == "test-model"
        assert agent.model.client is client

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

    async def test_empty_system_prompt_is_not_added_to_memory(self):
        class TextProvider:
            async def stream(self, _config, _tools, messages, _cancel):
                assert all(
                    part.part_kind != "system-prompt"
                    for message in messages
                    for part in message.parts
                )
                yield StreamEvent(
                    "text_delta", {"delta": "answer", "accumulated": "answer"}
                )
                yield StreamEvent("response_completed")

        tools = ToolRegistry()
        agent = Agent(ResponsesProvider("model"), tools, "")
        agent.model = TextProvider()
        memory = ContextManager()
        ctx = AgentContext(
            AthenaThread(
                thread_id="thread:test",
                session_id="session:test",
                status="running",
                context_ref="context:test",
            ),
            AthenaTurn(
                turn_id="turn:test",
                thread_id="thread:test",
                request_ref="question",
                status="running",
            ),
            lambda *_args: asyncio.sleep(0),
            tools,
            asyncio.Event(),
            memory,
        )

        await agent.run(ctx)

        assert all(
            part.part_kind != "system-prompt"
            for message in memory.items
            for part in message.parts
        )

    async def test_response_completed_closes_provider_stream_immediately(self):
        class ClosingProbeProvider:
            def __init__(self):
                self.closed = False
                self.generator = None

            def stream(self, *_args):
                async def events():
                    try:
                        yield StreamEvent(
                            "text_delta", {"delta": "done", "accumulated": "done"}
                        )
                        yield StreamEvent("response_completed")
                    finally:
                        self.closed = True

                self.generator = events()
                return self.generator

        tools = ToolRegistry()
        provider = ClosingProbeProvider()
        agent = Agent(ResponsesProvider("model"), tools, "system")
        agent.model = provider
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

        assert provider.closed
        assert provider.generator is not None
        assert provider.generator.ag_frame is None

    async def test_provider_error_is_not_reported_as_success(self):
        class ErrorProvider:
            async def stream(self, *_args):
                yield StreamEvent(kind="error", data={"message": "provider failed"})

        tools = ToolRegistry()
        agent = Agent(ResponsesProvider("model"), tools, "system")
        agent.model = ErrorProvider()
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
        agent = Agent(ResponsesProvider("model"), tools, "system")
        agent.model = CallsProvider()
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
                self.tools = ToolRegistry()
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

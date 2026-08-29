"""Agent 抽象层的单元测试。"""

import asyncio
import json
from dataclasses import fields
from inspect import signature

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
import pytest
import httpx
from openai import BadRequestError

import athena.core as core_api
import athena.core.agent as agent_api
from athena.core.agent import settings
from athena.core.agent.models import (
    AgentConfig,
    AgentContext,
    AgentOutcome,
    StepOutcome,
    ToolCall,
)
from athena.core.agent.provider import (
    DeepSeekProvider,
    OpenAIProvider,
    ResponsesProvider,
    StreamEvent,
    create_provider,
)
from athena.core.agent.runtime import (
    Agent,
    BaseAgent,
    _validate_structured_text,
    agent_runner,
    create_agent,
    create_code_agent,
)
from athena.core.agent.tools import RequestUserInputTool
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
    assert agent_api.AgentOutcome is AgentOutcome
    assert agent_api.BaseAgent is BaseAgent
    assert agent_api.ResponsesProvider is ResponsesProvider
    assert agent_api.StepOutcome is StepOutcome
    assert agent_api.StreamEvent is StreamEvent
    assert agent_api.ToolCall is ToolCall
    assert agent_api.agent_runner is agent_runner
    assert agent_api.create_agent is create_agent
    assert agent_api.create_code_agent is create_code_agent

    assert agent_api.create_provider is create_provider
    assert agent_api.OpenAIProvider is OpenAIProvider
    assert agent_api.DeepSeekProvider is DeepSeekProvider

    assert core_api.Agent is Agent
    assert core_api.AgentConfig is AgentConfig
    assert core_api.AgentContext is AgentContext
    assert core_api.AgentOutcome is AgentOutcome
    assert core_api.BaseAgent is BaseAgent
    assert core_api.StreamEvent is StreamEvent
    assert core_api.ToolCall is ToolCall
    assert core_api.agent_runner is agent_runner
    assert core_api.create_agent is create_agent


def test_code_agent_interface_has_four_parameters_and_five_fields() -> None:
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
        "tool_choice",
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
        set(vars(agent))
        == {
            "model",
            "tools",
            "system_prompt",
            "config",
            "_output_type",
            "_artifacts",
            "_structured_repair_provider",
        }
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
            async def stream(self, _config, _tools, messages, _cancel, **_kwargs):
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

            def stream(self, *_args, **_kwargs):
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
            async def stream(self, *_args, **_kwargs):
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

    async def test_function_call_is_emitted_before_tool_output_and_next_text(self):
        class ProbeTool(BaseTool):
            spec = ToolSpec(name="probe", description="probe", input_schema={})

            async def execute(self, input: dict, ctx: ToolContext):
                return "tool result"

        class ToolThenTextProvider:
            def __init__(self):
                self.calls = 0

            async def stream(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    yield StreamEvent(
                        "function_call",
                        {
                            "call_id": "call-1",
                            "name": "probe",
                            "arguments": {"path": "data.csv"},
                        },
                    )
                else:
                    yield StreamEvent(
                        "text_delta",
                        {"delta": "continue", "accumulated": "continue"},
                    )
                yield StreamEvent("response_completed")

        tools = ToolRegistry()
        tools.register(ProbeTool())
        agent = Agent(ResponsesProvider("model"), tools, "system")
        agent.model = ToolThenTextProvider()
        emitted: list[tuple[str, dict | None]] = []

        async def emit(kind, _event_ref, data=None):
            emitted.append((kind, data))

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
            emit,
            tools,
            asyncio.Event(),
        )

        await agent.run(ctx)

        visible = [
            (kind, data)
            for kind, data in emitted
            if kind
            in {"agent/function_call", "tool/begin", "tool/end", "agent/text_delta"}
        ]
        assert [kind for kind, _data in visible] == [
            "agent/function_call",
            "tool/begin",
            "tool/end",
            "agent/text_delta",
        ]
        assert visible[0][1] == {
            "name": "probe",
            "arguments": {"path": "data.csv"},
        }

    async def test_reasoning_is_retained_privately_for_tool_continuation(self):
        class ProbeTool(BaseTool):
            spec = ToolSpec(name="probe", description="probe", input_schema={})

            async def execute(self, input: dict, ctx: ToolContext):
                return "tool result"

        class ReasoningToolProvider:
            def __init__(self):
                self.calls = 0

            async def stream(self, _config, _tools, messages, _cancel, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    yield StreamEvent(
                        "reasoning_delta",
                        {"delta": "private thought", "accumulated": "private thought"},
                    )
                    yield StreamEvent(
                        "function_call",
                        {"call_id": "call-1", "name": "probe", "arguments": {}},
                    )
                else:
                    thinking = [
                        part
                        for message in messages
                        for part in message.parts
                        if isinstance(part, ThinkingPart)
                    ]
                    assert [part.content for part in thinking] == ["private thought"]
                    yield StreamEvent(
                        "text_delta", {"delta": "done", "accumulated": "done"}
                    )
                yield StreamEvent("response_completed")

        tools = ToolRegistry()
        tools.register(ProbeTool())
        provider = ReasoningToolProvider()
        agent = Agent(ResponsesProvider("model"), tools, "system")
        agent.model = provider
        emitted: list[str] = []

        async def emit(kind, _event_ref, _data=None):
            emitted.append(kind)

        ctx = AgentContext(
            AthenaThread(
                thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
            ),
            AthenaTurn(
                turn_id="t1.1",
                thread_id="t1",
                request_ref="request",
                status="running",
            ),
            emit,
            tools,
            asyncio.Event(),
        )

        await agent.run(ctx)

        assert provider.calls == 2
        assert "agent/reasoning_delta" not in emitted

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

            async def stream(self, *_args, **_kwargs):
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


def test_chat_completions_tool_schema_is_nested() -> None:
    spec = ToolSpec(name="echo", description="echo", input_schema={"type": "object"})

    assert spec.to_openai_tool() == {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "echo",
            "parameters": {"type": "object"},
        },
    }


class _CaptureClient:
    def __init__(self):
        self.kwargs: dict = {}

    @property
    def chat(self):
        class _Completions:
            def __init__(self, owner):
                self._owner = owner

            async def create(self, **kw):
                self._owner.kwargs = kw
                chunks = [type("C", (), {"choices": []})()]

                async def gen():
                    for c in chunks:
                        yield c

                return gen()

        class _Chat:
            def __init__(self, owner):
                self._owner = owner

            @property
            def completions(self):
                return _Completions(self._owner)

        return _Chat(self)


class _Out(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_stream_sets_response_format_when_output_type_given() -> None:
    client = _CaptureClient()
    provider = OpenAIProvider("model", client=client)
    events = [
        e
        async for e in provider.stream(
            AgentConfig(), ToolRegistry(), [], asyncio.Event(), output_type=_Out
        )
    ]
    rf = client.kwargs["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "_Out"
    assert rf["json_schema"]["schema"] == _Out.model_json_schema()
    assert any(e.kind == "response_completed" for e in events)


@pytest.mark.asyncio
async def test_stream_omits_response_format_without_output_type() -> None:
    client = _CaptureClient()
    provider = ResponsesProvider("model", client=client)
    events = [
        e
        async for e in provider.stream(
            AgentConfig(), ToolRegistry(), [], asyncio.Event()
        )
    ]
    assert "response_format" not in client.kwargs
    assert any(e.kind == "response_completed" for e in events)


def _registry_with_one_tool() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(_EchoTool())
    return tools


@pytest.mark.asyncio
async def test_an_agent_with_tools_keeps_them_instead_of_the_strict_schema() -> None:
    """有工具时不能再发 response_format，否则模型一个 tool call 也发不出来。

    实测：qwen 兼容端点在 ``tools + response_format`` 下 3/3 直接返回终态 JSON，去掉
    ``response_format`` 后 3/3 正常调工具。Athena 每个 Agent 都带 ``output_type``，
    这条一旦回归，整个 loop 会退化成"只写 JSON 不干活"。
    """
    client = _CaptureClient()
    provider = OpenAIProvider("model", client=client)

    events = [
        e
        async for e in provider.stream(
            AgentConfig(),
            _registry_with_one_tool(),
            [],
            asyncio.Event(),
            output_type=_Out,
        )
    ]

    assert "response_format" not in client.kwargs
    assert [t["function"]["name"] for t in client.kwargs["tools"]] == ["echo"]
    schema_message = client.kwargs["messages"][-1]
    assert schema_message["role"] == "system"
    assert json.dumps(_Out.model_json_schema()) in schema_message["content"]
    assert any(e.kind == "response_completed" for e in events)


@pytest.mark.asyncio
async def test_deepseek_with_tools_also_drops_the_json_object_constraint() -> None:
    client = _CaptureClient()
    provider = DeepSeekProvider("model", client=client)

    await anext(
        provider.stream(
            AgentConfig(),
            _registry_with_one_tool(),
            [],
            asyncio.Event(),
            output_type=_Out,
        )
    )

    assert "response_format" not in client.kwargs
    assert client.kwargs["messages"][-1]["role"] == "system"


class _ResponseFormatFallbackClient:
    def __init__(self, message: str) -> None:
        self.calls: list[dict] = []
        self.message = message

    @property
    def chat(self):
        owner = self

        class _Completions:
            async def create(self, **kwargs):
                owner.calls.append(kwargs)
                if len(owner.calls) == 1:
                    request = httpx.Request("POST", "https://example.test/chat")
                    response = httpx.Response(400, request=request)
                    raise BadRequestError(
                        owner.message,
                        response=response,
                        body={"error": {"message": owner.message}},
                    )

                async def chunks():
                    yield type("Chunk", (), {"choices": []})()

                return chunks()

        class _Chat:
            completions = _Completions()

        return _Chat()


@pytest.mark.asyncio
async def test_stream_retries_without_unsupported_response_format() -> None:
    client = _ResponseFormatFallbackClient(
        "This response_format type is unavailable now"
    )
    provider = OpenAIProvider("model", client=client)

    events = [
        event
        async for event in provider.stream(
            AgentConfig(), ToolRegistry(), [], asyncio.Event(), output_type=_Out
        )
    ]

    assert len(client.calls) == 2
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    assert "response_format" not in client.calls[1]
    assert any(event.kind == "response_completed" for event in events)


@pytest.mark.asyncio
async def test_stream_does_not_retry_unrelated_bad_request() -> None:
    client = _ResponseFormatFallbackClient("invalid model")
    provider = ResponsesProvider("model", client=client)

    with pytest.raises(BadRequestError, match="invalid model"):
        _ = [
            event
            async for event in provider.stream(
                AgentConfig(), ToolRegistry(), [], asyncio.Event(), output_type=_Out
            )
        ]

    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_stream_disables_deepseek_thinking_for_tool_execution() -> None:
    """工具 Agent 禁用默认 thinking，避免推理耗尽输出预算却未调用工具。"""
    client = _CaptureClient()
    provider = ResponsesProvider("model", client=client)

    await anext(provider.stream(AgentConfig(), ToolRegistry(), [], asyncio.Event()))

    assert client.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_stream_can_require_a_tool_call() -> None:
    client = _CaptureClient()
    provider = ResponsesProvider("model", client=client)
    tools = ToolRegistry()
    tools.register(_EchoTool())

    await anext(
        provider.stream(
            AgentConfig(tool_choice="required"),
            tools,
            [],
            asyncio.Event(),
        )
    )

    assert client.kwargs["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_deepseek_stream_injects_schema_and_uses_json_object() -> None:
    client = _CaptureClient()
    provider = DeepSeekProvider("model", client=client)

    events = [
        e
        async for e in provider.stream(
            AgentConfig(), ToolRegistry(), [], asyncio.Event(), output_type=_Out
        )
    ]

    assert client.kwargs["response_format"] == {"type": "json_object"}
    msgs = client.kwargs["messages"]
    assert msgs and msgs[-1]["role"] == "system"
    assert "Return a JSON object matching this schema" in msgs[-1]["content"]
    assert json.dumps(_Out.model_json_schema()) in msgs[-1]["content"]
    assert any(e.kind == "response_completed" for e in events)


@pytest.mark.asyncio
async def test_openai_stream_does_not_inject_schema_message() -> None:
    client = _CaptureClient()
    provider = OpenAIProvider("model", client=client)

    await anext(
        provider.stream(
            AgentConfig(), ToolRegistry(), [], asyncio.Event(), output_type=_Out
        )
    )

    assert client.kwargs["messages"] == []
    assert client.kwargs["response_format"]["type"] == "json_schema"


def test_create_provider_routes_by_llm_provider_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    assert isinstance(create_provider("m"), DeepSeekProvider)

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert isinstance(create_provider("m"), OpenAIProvider)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        create_provider("m")

    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        create_provider("m")


def test_settings_provider_kind_default_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert settings.provider_kind() == "deepseek"

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert settings.provider_kind() == "openai"

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        settings.provider_kind()

    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        settings.provider_kind()


class _StructuredOut(BaseModel):
    answer: str


class _NonEmptyStructuredBatch(BaseModel):
    hypotheses: list[str] = Field(min_length=1)


class _StructuredProvider:
    def __init__(self):
        self.calls = 0

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                "text_delta", {"delta": "not json", "accumulated": "not json"}
            )
        else:
            yield StreamEvent(
                "text_delta",
                {"delta": '{"answer":"hi"}', "accumulated": '{"answer":"hi"}'},
            )
        yield StreamEvent("response_completed")


@pytest.mark.asyncio
async def test_agent_structured_output_validates_retries_and_persists(
    tmp_path,
) -> None:
    from athena.core.artifact_store import LocalArtifactStore
    from athena.core.agent.models import AgentContext

    store = LocalArtifactStore(tmp_path / "artifacts")
    tools = ToolRegistry()
    agent = Agent(
        ResponsesProvider("model"),
        tools,
        "system",
        output_type=_StructuredOut,
        artifacts=store,
    )
    agent.model = _StructuredProvider()
    ctx = AgentContext(
        AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
        ),
        lambda *_a: asyncio.sleep(0),
        tools,
        asyncio.Event(),
    )

    outcome = await agent.run(ctx)

    assert outcome.result_ref.startswith("sha256:")
    assert (await store.get_text(outcome.result_ref)) == '{"answer":"hi"}'


class _AlwaysInvalidStructuredProvider:
    def __init__(self):
        self.calls = 0

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        yield StreamEvent(
            "text_delta", {"delta": "not json", "accumulated": "not json"}
        )
        yield StreamEvent("response_completed")


class _ValidStructuredProvider:
    def __init__(self):
        self.calls = 0

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        yield StreamEvent(
            "text_delta",
            {"delta": '{"answer":"hi"}', "accumulated": '{"answer":"hi"}'},
        )
        yield StreamEvent("response_completed")


class _FencedStructuredProvider:
    """模型把终态 JSON 包进 markdown 围栏——真机上 Ideator 就是这么打死 SEARCH 的。"""

    def __init__(self, body: str) -> None:
        self.calls = 0
        self.body = body

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        yield StreamEvent("text_delta", {"delta": self.body, "accumulated": self.body})
        yield StreamEvent("response_completed")


class _RecordingStructuredRepairProvider:
    """Tool-free structured repair fake that records the supplied contract."""

    model_name = "structured-repair-test"

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls = 0
        self.configs = []
        self.tool_names: list[tuple[str, ...]] = []
        self.output_types: list[type | None] = []

    async def stream(
        self,
        config,
        tools,
        _messages,
        _cancel,
        *,
        output_type=None,
    ):
        self.calls += 1
        self.configs.append(config)
        self.tool_names.append(tuple(spec.name for spec in tools.specs))
        self.output_types.append(output_type)
        yield StreamEvent("text_delta", {"delta": self.body, "accumulated": self.body})
        yield StreamEvent("response_completed")


def _structured_context(tools: ToolRegistry) -> AgentContext:
    return AgentContext(
        AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
        ),
        lambda *_a: asyncio.sleep(0),
        tools,
        asyncio.Event(),
    )


@pytest.mark.parametrize(
    "body",
    [
        '```json\n{"answer":"hi"}\n```',
        '```\n{"answer":"hi"}\n```',
        '```json\r\n{"answer":"hi"}\r\n```',
    ],
)
@pytest.mark.asyncio
async def test_a_fenced_json_answer_is_accepted_on_the_first_try(body: str) -> None:
    """围栏是格式噪声，不该烧掉重试预算——一次采样就要通过。"""
    tools = ToolRegistry()
    agent = Agent(
        ResponsesProvider("model"), tools, "system", output_type=_StructuredOut
    )
    agent.model = _FencedStructuredProvider(body)

    outcome = await agent.run(_structured_context(tools))

    assert outcome.result_ref
    assert agent.model.calls == 1


@pytest.mark.asyncio
async def test_opted_in_structured_repair_accepts_embedded_json() -> None:
    """Opt-in agents may recover valid JSON embedded in explanatory prose."""
    tools = ToolRegistry()
    provider = _FencedStructuredProvider(
        'Formatting note: {"answer":"hi"} End of explanation.'
    )
    repair = _RecordingStructuredRepairProvider("not used")
    agent = Agent(
        provider,
        tools,
        "system",
        output_type=_StructuredOut,
        structured_repair_provider=repair,
    )

    outcome = await agent.run(_structured_context(tools))

    assert outcome.result_ref
    assert provider.calls == 1
    assert repair.calls == 0


@pytest.mark.asyncio
async def test_structured_repair_runs_tool_free_without_replaying_agent(
    tmp_path,
) -> None:
    """The repair pass receives no tools and replaces no agent experiment."""
    from athena.core.artifact_store import LocalArtifactStore

    store = LocalArtifactStore(tmp_path / "artifacts")
    tools = ToolRegistry()
    provider = _FencedStructuredProvider("answer repaired (not JSON)")
    repair = _RecordingStructuredRepairProvider('{"answer":"repaired"}')
    agent = Agent(
        provider,
        tools,
        "system",
        output_type=_StructuredOut,
        artifacts=store,
        structured_repair_provider=repair,
    )

    outcome = await agent.run(_structured_context(tools))

    assert provider.calls == 1
    assert repair.calls == 1
    assert repair.tool_names == [()]
    assert repair.output_types == [_StructuredOut]
    assert await store.get_text(outcome.result_ref) == '{"answer":"repaired"}'


@pytest.mark.asyncio
async def test_structured_repair_invalid_output_fails_explicitly() -> None:
    """An invalid format-only response must fail closed without a virtual result."""
    tools = ToolRegistry()
    provider = _AlwaysInvalidStructuredProvider()
    repair = _RecordingStructuredRepairProvider("still not json")
    agent = Agent(
        provider,
        tools,
        "system",
        output_type=_StructuredOut,
        structured_repair_provider=repair,
    )

    with pytest.raises(RuntimeError, match="semantic correction failed"):
        await agent.run(_structured_context(tools))

    assert provider.calls == 2
    assert repair.calls == 1
    assert repair.tool_names == [()]
    assert repair.output_types == [_StructuredOut]


@pytest.mark.asyncio
async def test_structured_repair_rejects_ungrounded_scalar() -> None:
    """Repair cannot invent a schema-valid value absent from the source text."""
    tools = ToolRegistry()
    provider = _AlwaysInvalidStructuredProvider()
    repair = _RecordingStructuredRepairProvider('{"answer":"fabricated"}')
    agent = Agent(
        provider,
        tools,
        "system",
        output_type=_StructuredOut,
        structured_repair_provider=repair,
    )

    with pytest.raises(RuntimeError, match="semantic correction failed"):
        await agent.run(_structured_context(tools))

    assert provider.calls == 2
    assert repair.calls == 1


@pytest.mark.asyncio
async def test_semantic_structured_retry_repairs_an_empty_required_batch(
    tmp_path,
) -> None:
    """A non-empty semantic field is corrected without exposing any tools."""

    from athena.core.artifact_store import LocalArtifactStore

    class EmptyThenValidProvider:
        model_name = "semantic-correction-test"

        def __init__(self) -> None:
            self.calls = 0
            self.tool_names: list[tuple[str, ...]] = []
            self.output_types: list[type | None] = []
            self.max_tokens: list[int] = []
            self.messages = []

        async def stream(self, config, tools, messages, _cancel, *, output_type=None):
            self.calls += 1
            self.max_tokens.append(config.max_tokens)
            self.tool_names.append(tuple(spec.name for spec in tools.specs))
            self.output_types.append(output_type)
            self.messages.append(messages)
            body = (
                '{"hypotheses":[]}'
                if self.calls == 1
                else '{"hypotheses":["use calibrated probabilities"]}'
            )
            yield StreamEvent("text_delta", {"delta": body, "accumulated": body})
            yield StreamEvent("response_completed")

    provider = EmptyThenValidProvider()
    repair = _RecordingStructuredRepairProvider('{"hypotheses":[]}')
    tools = ToolRegistry()
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = Agent(
        provider,
        tools,
        "system",
        output_type=_NonEmptyStructuredBatch,
        artifacts=store,
        structured_repair_provider=repair,
    )

    outcome = await agent.run(_structured_context(tools))

    assert provider.calls == 2
    assert provider.tool_names == [(), ()]
    assert provider.output_types == [_NonEmptyStructuredBatch, None]
    assert provider.max_tokens == [4096, 8192]
    assert repair.calls == 1
    correction_text = "\n".join(
        str(part.content)
        for message in provider.messages[-1]
        for part in message.parts
        if hasattr(part, "content")
    )
    assert 'Previous final answer:\n{"hypotheses":[]}' in correction_text
    assert "List should have at least 1 item" in correction_text
    assert await store.get_text(outcome.result_ref) == (
        '{"hypotheses":["use calibrated probabilities"]}'
    )


@pytest.mark.asyncio
async def test_semantic_structured_retry_disables_thinking_for_correction(
    monkeypatch, tmp_path
) -> None:
    """The bounded Pro correction must reserve tokens for visible JSON."""

    from athena.core.artifact_store import LocalArtifactStore

    class ThinkingProvider:
        model_name = "deepseek-pro-test"
        thinking_enabled = True
        client = object()

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
            self.calls += 1
            body = '{"hypotheses":[]}'
            yield StreamEvent("text_delta", {"delta": body, "accumulated": body})
            yield StreamEvent("response_completed")

    correction = _RecordingStructuredRepairProvider(
        '{"hypotheses":["use calibrated probabilities"]}'
    )
    created: list[tuple[str, object]] = []

    def create_non_thinking(model, *, client=None):
        created.append((model, client))
        return correction

    monkeypatch.setattr(
        "athena.core.agent.runtime.create_provider", create_non_thinking
    )
    provider = ThinkingProvider()
    repair = _RecordingStructuredRepairProvider('{"hypotheses":[]}')
    tools = ToolRegistry()
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = Agent(
        provider,
        tools,
        "system",
        output_type=_NonEmptyStructuredBatch,
        artifacts=store,
        structured_repair_provider=repair,
    )

    outcome = await agent.run(_structured_context(tools))

    assert provider.calls == 1
    assert created == [("deepseek-pro-test", provider.client)]
    assert correction.calls == 1
    assert correction.output_types == [None]
    assert correction.configs[0].max_tokens == 8192
    assert await store.get_text(outcome.result_ref) == (
        '{"hypotheses":["use calibrated probabilities"]}'
    )


def test_semantic_correction_history_removes_tool_protocol_parts() -> None:
    from athena.core.agent.runtime import _semantic_correction_history

    history = [
        ModelRequest(parts=[UserPromptPart(content="analyze the dataset")]),
        ModelResponse(
            parts=[
                ThinkingPart(content="private reasoning"),
                ToolCallPart(
                    tool_name="shell_command",
                    args='{"command":"probe"}',
                    tool_call_id="call-1",
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="shell_command",
                    content="AUROC evidence: 0.7434",
                    tool_call_id="call-1",
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content='{"hypotheses":[]}')]),
    ]

    cleaned = _semantic_correction_history(history)
    parts = [part for message in cleaned for part in message.parts]

    assert not any(
        isinstance(part, (ThinkingPart, ToolCallPart, ToolReturnPart)) for part in parts
    )
    visible = "\n".join(str(part.content) for part in parts if hasattr(part, "content"))
    assert "analyze the dataset" in visible
    assert "Completed tool evidence (shell_command; outcome=success)" in visible
    assert "AUROC evidence: 0.7434" in visible


def test_embedded_structured_output_rejects_ambiguous_valid_objects() -> None:
    """Two distinct schema-valid objects are ambiguous and must not be guessed."""
    with pytest.raises(ValidationError):
        _validate_structured_text(
            _StructuredOut,
            'draft {"answer":"first"} final {"answer":"second"}',
            allow_embedded=True,
        )


def test_opted_in_structured_parser_wraps_one_required_top_level_list() -> None:
    """A bare list is recoverable only for a schema with one required field."""

    instance = _validate_structured_text(
        _NonEmptyStructuredBatch,
        '["use calibrated probabilities"]',
        allow_embedded=True,
    )

    assert instance.hypotheses == ["use calibrated probabilities"]


@pytest.mark.asyncio
async def test_format_repair_does_not_repeat_prior_tool_work(tmp_path) -> None:
    """An invalid final answer repairs immediately after one completed tool call."""
    from athena.core.artifact_store import LocalArtifactStore

    executions = 0

    class CountingTool(BaseTool):
        spec = ToolSpec(name="probe", description="probe", input_schema={})

        async def execute(self, input: dict, ctx: ToolContext):
            nonlocal executions
            executions += 1
            return "answer repaired"

    class ToolThenInvalidProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    "function_call",
                    {"call_id": "probe-1", "name": "probe", "arguments": {}},
                )
            elif self.calls == 2:
                yield StreamEvent(
                    "text_delta",
                    {
                        "delta": "answer repaired (not JSON)",
                        "accumulated": "answer repaired (not JSON)",
                    },
                )
            else:  # pragma: no cover - immediate repair must prevent this call
                yield StreamEvent(
                    "function_call",
                    {"call_id": "probe-2", "name": "probe", "arguments": {}},
                )
            yield StreamEvent("response_completed")

    tools = ToolRegistry()
    tools.register(CountingTool())
    provider = ToolThenInvalidProvider()
    repair = _RecordingStructuredRepairProvider('{"answer":"repaired"}')
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = Agent(
        provider,
        tools,
        "system",
        output_type=_StructuredOut,
        artifacts=store,
        structured_repair_provider=repair,
    )

    outcome = await agent.run(_structured_context(tools))

    assert provider.calls == 2
    assert executions == 1
    assert repair.calls == 1
    assert await store.get_text(outcome.result_ref) == '{"answer":"repaired"}'


@pytest.mark.asyncio
async def test_prose_around_the_fence_still_counts_as_invalid() -> None:
    """只剥围栏，不去猜正文里哪一段是 JSON；真的乱答仍要走重试并最终报错。"""
    tools = ToolRegistry()
    agent = Agent(
        ResponsesProvider("model"), tools, "system", output_type=_StructuredOut
    )
    agent.model = _FencedStructuredProvider('here you go: {"answer": ')

    with pytest.raises(RuntimeError, match="structured output invalid"):
        await agent.run(_structured_context(tools))


@pytest.mark.asyncio
async def test_agent_structured_output_raises_after_max_retries() -> None:
    """无效 JSON 重试耗尽 → RuntimeError，而非回退未校验的虚拟 ref。"""
    tools = ToolRegistry()
    agent = Agent(
        ResponsesProvider("model"),
        tools,
        "system",
        output_type=_StructuredOut,
    )
    agent.model = _AlwaysInvalidStructuredProvider()
    ctx = AgentContext(
        AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
        ),
        lambda *_a: asyncio.sleep(0),
        tools,
        asyncio.Event(),
    )

    with pytest.raises(RuntimeError, match="structured output invalid"):
        await agent.run(ctx)
    assert agent.model.calls == 4  # 1 次初始采样 + 3 次重试，未超过上限


@pytest.mark.asyncio
async def test_agent_structured_output_without_artifacts_uses_virtual_ref() -> None:
    """artifacts=None 时结构化输出落为虚拟 result:// ref，不持久化。"""
    tools = ToolRegistry()
    agent = Agent(
        ResponsesProvider("model"),
        tools,
        "system",
        output_type=_StructuredOut,
    )
    agent.model = _ValidStructuredProvider()
    ctx = AgentContext(
        AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
        ),
        lambda *_a: asyncio.sleep(0),
        tools,
        asyncio.Event(),
    )

    outcome = await agent.run(ctx)

    assert outcome.result_ref.startswith("result://")
    assert outcome.result_ref == f"result://{ctx.turn.turn_id}"


class _AskUserProvider:
    """第一轮输出 request_user_input 工具调用，第二轮输出最终文本。"""

    def __init__(self):
        self.calls = 0

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                "function_call",
                {
                    "call_id": "c1",
                    "name": "request_user_input",
                    "arguments": {"prompt": "请选择方案"},
                },
            )
        else:
            yield StreamEvent(
                "text_delta",
                {"delta": "好的，采用方案A", "accumulated": "好的，采用方案A"},
            )
        yield StreamEvent("response_completed")


def _tool_returns(memory):
    return [
        part
        for msg in memory.items
        for part in msg.parts
        if isinstance(part, ToolReturnPart)
    ]


@pytest.mark.asyncio
async def test_ask_user_tool_round_trips_answer_into_memory() -> None:
    """request_user_input 工具 await ask_user 阻塞 → 回答写回 memory → 循环继续。"""
    asked: list[str] = []

    async def ask_user(prompt: str) -> str | None:
        asked.append(prompt)
        return "方案A"

    tools = ToolRegistry()
    tools.register(RequestUserInputTool())
    agent = Agent(ResponsesProvider("model"), tools, "system")
    agent.model = _AskUserProvider()
    memory = ContextManager()
    ctx = AgentContext(
        AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
        ),
        lambda *_a: asyncio.sleep(0),
        tools,
        asyncio.Event(),
        memory,
        ask_user=ask_user,
    )

    outcome = await agent.run(ctx)

    assert asked == ["请选择方案"]
    returns = _tool_returns(memory)
    assert len(returns) == 1
    assert returns[0].content == "方案A"
    assert outcome.result_ref == "result://t1.1"


@pytest.mark.asyncio
async def test_request_user_input_passes_choices_to_ask_user() -> None:
    """有 choices 时工具把 choices / allow_custom / allow_skip 透传给 ask_user。"""
    seen: dict = {}

    async def ask_user(prompt, *, choices=None, allow_custom=True, allow_skip=True):
        seen.update(
            {
                "prompt": prompt,
                "choices": choices,
                "allow_custom": allow_custom,
                "allow_skip": allow_skip,
            }
        )
        return "choice:f1"

    tool = RequestUserInputTool()
    ctx = ToolContext(
        "request_user_input",
        "c1",
        lambda *_a: asyncio.sleep(0),
        asyncio.Event(),
        ask_user=ask_user,
    )
    result = await tool.execute(
        {
            "prompt": "choose metric",
            "choices": [{"label": "f1", "value": "f1"}],
            "allow_custom": False,
            "allow_skip": False,
        },
        ctx,
    )

    assert result == "choice:f1"
    assert seen["prompt"] == "choose metric"
    assert seen["choices"] == [{"label": "f1", "value": "f1"}]
    assert seen["allow_custom"] is False
    assert seen["allow_skip"] is False


@pytest.mark.asyncio
async def test_ask_user_tool_without_injection_reports_error() -> None:
    """未注入 ask_user 时工具返回错误文本，循环不崩溃、照常结束。"""
    tools = ToolRegistry()
    tools.register(RequestUserInputTool())
    agent = Agent(ResponsesProvider("model"), tools, "system")
    agent.model = _AskUserProvider()
    memory = ContextManager()
    ctx = AgentContext(
        AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
        ),
        lambda *_a: asyncio.sleep(0),
        tools,
        asyncio.Event(),
        memory,
    )

    outcome = await agent.run(ctx)

    returns = _tool_returns(memory)
    assert len(returns) == 1
    content = str(returns[0].content)
    assert "[ERROR]" in content
    assert "ask_user" in content
    assert outcome.result_ref == "result://t1.1"


def test_agent_runner_binds_ask_user_factory() -> None:
    """agent_runner 的 ask_user 工厂按 (thread, turn) 绑定注入 AgentContext。"""
    bound: list[tuple[str, str]] = []

    def make_ask_user(thread, turn):
        async def ask(_prompt: str) -> str | None:
            bound.append((thread.thread_id, turn.turn_id))
            return "ok"

        return ask

    tools = ToolRegistry()
    tools.register(RequestUserInputTool())
    agent = Agent(ResponsesProvider("model"), tools, "system")
    agent.model = _AskUserProvider()
    runner = agent_runner(agent, tools, ask_user=make_ask_user)

    thread = AthenaThread(
        thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
    )
    turn = AthenaTurn(
        turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
    )

    async def emit(_k, _r, _d=None):
        pass

    outcome = _arun(runner(thread, turn, emit))

    assert bound == [("t1", "t1.1")]
    assert outcome.result_ref == "result://t1.1"

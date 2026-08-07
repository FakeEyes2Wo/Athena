"""Tests for the reusable single-turn chat utility."""

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from athena.core.tool import ToolRegistry, tool
from athena.memory.context_manager import ContextManager
from athena.utils.single_turn_chat import single_turn_chat


@dataclass(frozen=True)
class ToolCallScript:
    name: str
    arguments: dict[str, object]


def tool_call(name: str, arguments: dict[str, object]) -> ToolCallScript:
    return ToolCallScript(name, arguments)


class ScriptedClient:
    def __init__(self, responses: list[str | None | ToolCallScript]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, ToolCallScript):
            function = SimpleNamespace(
                name=response.name,
                arguments=json.dumps(response.arguments),
            )
            delta = SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(index=0, id="call-1", function=function)],
            )
            finish_reason = "tool_calls"
        else:
            delta = SimpleNamespace(content=response, tool_calls=None)
            finish_reason = "stop"
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
        )

        async def stream():
            yield chunk

        return stream()


class BlockingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **_kwargs):
        self.started.set()
        await asyncio.Event().wait()


async def test_returns_final_text_without_reusing_implicit_history():
    client = ScriptedClient(["first answer", "second answer"])

    assert (
        await single_turn_chat("first", model="model", client=client) == "first answer"
    )
    assert (
        await single_turn_chat("second", model="model", client=client)
        == "second answer"
    )
    assert client.requests[1]["messages"] == [{"role": "user", "content": "second"}]


async def test_treats_artifact_like_prompt_as_literal_text():
    client = ScriptedClient(["answer"])

    result = await single_turn_chat(
        "artifact://README.md", model="model", client=client
    )

    assert result == "answer"
    assert client.requests[0]["messages"] == [
        {"role": "user", "content": "artifact://README.md"}
    ]


async def test_reuses_caller_owned_memory():
    client = ScriptedClient(["first answer", "second answer"])
    memory = ContextManager()

    await single_turn_chat("first", model="model", client=client, memory=memory)
    result = await single_turn_chat(
        "second", model="model", client=client, memory=memory
    )

    assert result == "second answer"
    assert client.requests[1]["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
    ]


@pytest.mark.parametrize("system_prompt", [None, ""])
async def test_omits_empty_system_prompt(system_prompt):
    client = ScriptedClient(["answer"])

    await single_turn_chat(
        "question", model="model", client=client, system_prompt=system_prompt
    )

    assert client.requests[0]["messages"] == [{"role": "user", "content": "question"}]


async def test_adds_nonempty_system_prompt_once_to_reused_memory():
    client = ScriptedClient(["first answer", "second answer"])
    memory = ContextManager()

    await single_turn_chat(
        "first",
        model="model",
        client=client,
        memory=memory,
        system_prompt="Be concise.",
    )
    await single_turn_chat(
        "second",
        model="model",
        client=client,
        memory=memory,
        system_prompt="Be concise.",
    )

    assert client.requests[1]["messages"] == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
    ]


async def test_executes_tools_and_forwards_events():
    tools = ToolRegistry()
    calls: list[str] = []

    @tool(
        name="echo",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
    )
    async def echo(text: str) -> str:
        calls.append(text)
        return text

    tools.register(echo)
    client = ScriptedClient([tool_call("echo", {"text": "value"}), "final answer"])
    events: list[tuple[str, str, dict | None]] = []

    async def emit(kind, ref, data=None):
        events.append((kind, ref, data))

    result = await single_turn_chat(
        "question", model="model", client=client, tools=tools, emit=emit
    )

    assert result == "final answer"
    assert calls == ["value"]
    assert [event[0] for event in events] == [
        "tool/begin",
        "tool/end",
        "agent/text_delta",
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"prompt": "", "model": "model"}, "prompt"),
        ({"prompt": "question", "model": ""}, "model"),
        ({"prompt": "question", "model": "model", "max_turns": 0}, "max_turns"),
        ({"prompt": "question", "model": "model", "max_turns": True}, "max_turns"),
        ({"prompt": "question", "model": "model", "max_tokens": 0}, "max_tokens"),
        ({"prompt": "question", "model": "model", "max_tokens": True}, "max_tokens"),
        (
            {"prompt": "question", "model": "model", "temperature": 2.1},
            "temperature",
        ),
        (
            {"prompt": "question", "model": "model", "temperature": True},
            "temperature",
        ),
    ],
)
async def test_rejects_invalid_input_before_model_call(kwargs, message):
    client = ScriptedClient(["unused"])

    with pytest.raises(ValueError, match=message):
        await single_turn_chat(client=client, **kwargs)

    assert client.requests == []


async def test_rejects_pre_cancelled_call_before_model_execution():
    client = ScriptedClient(["unused"])
    cancel = asyncio.Event()
    cancel.set()

    with pytest.raises(asyncio.CancelledError):
        await single_turn_chat("question", model="model", client=client, cancel=cancel)

    assert client.requests == []


async def test_does_not_return_old_answer_when_current_call_has_no_text():
    memory = ContextManager()
    memory.append(ModelResponse(parts=[TextPart(content="old answer")]))
    client = ScriptedClient([None])

    with pytest.raises(RuntimeError, match="final assistant text"):
        await single_turn_chat("question", model="model", client=client, memory=memory)


async def test_task_cancellation_propagates():
    client = BlockingClient()
    task = asyncio.create_task(
        single_turn_chat("question", model="model", client=client)
    )
    await asyncio.wait_for(client.started.wait(), timeout=0.1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

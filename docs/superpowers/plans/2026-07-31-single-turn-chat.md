# Single-Turn Chat Utility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an asynchronous `/btw`-style chat helper that reuses Athena's Agent loop and optionally updates caller-owned conversation memory.

**Architecture:** The helper creates temporary Athena thread/turn DTOs and invokes the existing `Agent` with an `AgentContext`; it does not start `ThreadRuntime` or persist rollout data. A supplied `ContextManager` is mutated in place, while an omitted one is ephemeral. Final text is extracted only from assistant messages created by the current invocation.

**Tech Stack:** Python 3.11+, asyncio, OpenAI-compatible streaming client, PydanticAI messages, pytest/pytest-asyncio

## Global Constraints

- `system_prompt=None` and `system_prompt=""` inject no system message.
- `memory=None` provides stateless behavior; a supplied `ContextManager` remains caller-owned and accumulates the turn.
- Reuse the existing Agent provider and tool loop; do not create app-server queues, Threads, journals, artifacts, or rollout records.
- Return the current invocation's final assistant text as `str`.
- Treat every `prompt` as literal user text, including values beginning with
  `artifact://`; preserve artifact resolution for normal app-server Agent contexts.
- Require non-empty prompt and model values, positive integer turn/token limits, and temperature in the inclusive `0..2` range.

---

### Task 1: Empty System Prompt Semantics

**Files:**
- Modify: `src/athena/core/agent/agent.py`
- Test: `test/unit/test_agent.py`

**Interfaces:**
- Consumes: `Agent.run(ctx: AgentContext) -> AgentOutcome`
- Produces: `Agent.run()` appends `SystemPromptPart` only when `system_prompt` is non-empty and memory lacks a system prompt.

- [ ] **Step 1: Write the failing test**

Add a provider that records the real PydanticAI messages passed by `Agent.run()` and returns a final response:

```python
async def test_empty_system_prompt_is_not_added_to_memory(self):
    class TextProvider:
        async def stream(self, _config, _tools, messages, _cancel):
            assert all(
                part.part_kind != "system-prompt"
                for message in messages
                for part in message.parts
            )
            yield StreamEvent("text_delta", {"delta": "answer", "accumulated": "answer"})
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
```

- [ ] **Step 2: Run the test to verify RED**

Run: `.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q test/unit/test_agent.py::TestBaseAgent::test_empty_system_prompt_is_not_added_to_memory`

Expected: FAIL because the current guard appends `SystemPromptPart(content="")`.

- [ ] **Step 3: Implement the minimal guard**

Change the system injection branch in `Agent.run()`:

```python
if self.system_prompt and not _has_system(mem):
    mem.append(ModelRequest(parts=[SystemPromptPart(content=self.system_prompt)]))
```

- [ ] **Step 4: Run the focused Agent tests**

Run: `.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q test/unit/test_agent.py`

Expected: PASS.

- [ ] **Step 5: Commit the kernel behavior**

Run: `git add src/athena/core/agent/agent.py test/unit/test_agent.py`

Run: `git commit -m "fix(agent): skip empty system prompts"`

---

### Task 2: Single-Turn Chat Helper

**Files:**
- Create: `src/athena/utils/single_turn_chat.py`
- Modify: `src/athena/utils/__init__.py`
- Create: `test/unit/test_single_turn_chat.py`

**Interfaces:**
- Consumes: `create_agent()`, `AgentContext`, `ContextManager`, `ToolRegistry`, `EmitEvent`, `AthenaThread`, and `AthenaTurn`.
- Produces: `single_turn_chat(prompt: str, *, model: str, memory: ContextManager | None = None, tools: ToolRegistry | None = None, system_prompt: str | None = None, client: AsyncOpenAI | None = None, max_turns: int = 20, max_tokens: int = 4096, temperature: float = 0.1, emit: EmitEvent | None = None, cancel: asyncio.Event | None = None) -> str`.
- Produces: `from athena.utils import single_turn_chat`.

- [ ] **Step 1: Write failing public-behavior tests**

Use a deterministic fake OpenAI client at the network boundary. Its
`chat.completions.create()` records each request and returns an async iterator of
OpenAI-shaped chunks. Test these behaviors against the real `ResponsesProvider` and
`Agent` loop:

```python
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
                tool_calls=[
                    SimpleNamespace(
                        index=0, id="call-1", function=function
                    )
                ],
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


async def test_returns_final_text_without_reusing_implicit_history():
    client = ScriptedClient(["first answer", "second answer"])

    assert await single_turn_chat("first", model="model", client=client) == "first answer"
    assert await single_turn_chat("second", model="model", client=client) == "second answer"
    assert client.requests[1]["messages"] == [{"role": "user", "content": "second"}]


async def test_reuses_caller_owned_memory():
    client = ScriptedClient(["first answer", "second answer"])
    memory = ContextManager()

    await single_turn_chat("first", model="model", client=client, memory=memory)
    result = await single_turn_chat("second", model="model", client=client, memory=memory)

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
    assert client.requests[0]["messages"] == [
        {"role": "user", "content": "question"}
    ]
```

Add focused cases proving a registered tool is executed before the final answer, emitted
Agent/tool events reach the supplied callback, blank/config-invalid inputs fail before
the client is called, a pre-set cancellation event raises `asyncio.CancelledError`, and
an empty provider completion raises `RuntimeError` even when old history contains an
assistant answer.

```python
async def test_executes_tools_and_forwards_events():
    tools = ToolRegistry()
    calls = []

    @tool(name="echo", input_schema={"type": "object", "properties": {"text": {"type": "string"}}})
    async def echo(text: str) -> str:
        calls.append(text)
        return text

    tools.register(echo)
    client = ScriptedClient([tool_call("echo", {"text": "value"}), "final answer"])
    events = []

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
        ({"prompt": "question", "model": "model", "max_tokens": 0}, "max_tokens"),
        ({"prompt": "question", "model": "model", "temperature": 2.1}, "temperature"),
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


async def test_does_not_return_an_old_answer_when_current_call_has_no_text():
    memory = ContextManager()
    memory.append(ModelResponse(parts=[TextPart(content="old answer")]))
    client = ScriptedClient([None])
    with pytest.raises(RuntimeError, match="final assistant text"):
        await single_turn_chat("question", model="model", client=client, memory=memory)
```

- [ ] **Step 2: Run the new module to verify RED**

Run: `.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q test/unit/test_single_turn_chat.py`

Expected: collection FAIL with `ModuleNotFoundError` because
`athena.utils.single_turn_chat` does not exist.

- [ ] **Step 3: Implement validation and orchestration**

Create `single_turn_chat.py` with the public signature above, plus private helpers:

```python
async def _noop_emit(_kind: str, _ref: str, _data: dict[str, Any] | None = None) -> None:
    return None


def _validate(
    prompt: str,
    model: str,
    max_turns: int,
    max_tokens: int,
    temperature: float,
) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        raise ValueError("max_turns must be a positive integer")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
        raise ValueError("temperature must be between 0 and 2")
```

Record `start_index = len(active_memory.items)`, create the Agent and temporary DTOs,
then run:

```python
ctx = AgentContext(
    thread=thread,
    turn=turn,
    emit=emit or _noop_emit,
    tools=active_tools,
    cancel=active_cancel,
    memory=active_memory,
    input_text=prompt,
)
await agent.run(ctx)
return _final_text(active_memory.items[start_index:])
```

`_final_text()` scans new messages in reverse and accepts only a `ModelResponse` whose
parts contain text and no `tool-call`; it joins the text parts and raises `RuntimeError`
when no non-empty final text exists. Check `active_cancel.is_set()` before model execution
and raise `asyncio.CancelledError`.

- [ ] **Step 4: Export the helper**

Replace the current `athena.utils` initializer with:

```python
"""Shared stateless and caller-state utility functions."""

from athena.utils.single_turn_chat import single_turn_chat

__all__ = ["single_turn_chat"]
```

- [ ] **Step 5: Run focused tests and refactor while green**

Run: `.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q test/unit/test_single_turn_chat.py test/unit/test_agent.py`

Expected: PASS with no warnings.

- [ ] **Step 6: Run app-server and complete unit regression**

Run: `.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q test/unit/app_server test/unit`

Expected: PASS with no new failures or warnings.

- [ ] **Step 7: Check formatting and diff quality**

Run: `.venv\Scripts\python.exe -m black --check src/athena/utils/single_turn_chat.py src/athena/utils/__init__.py src/athena/core/agent/agent.py test/unit/test_single_turn_chat.py test/unit/test_agent.py`

Run: `git diff --check`

Expected: both commands exit successfully.

- [ ] **Step 8: Commit the utility**

Run: `git add src/athena/utils/single_turn_chat.py src/athena/utils/__init__.py test/unit/test_single_turn_chat.py`

Run: `git commit -m "feat(utils): add single-turn chat helper"`

"""Single-turn text and structured Agent calls with shared context construction."""

import asyncio
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse

from athena.core.agent.models import AgentConfig, AgentContext
from athena.core.agent.provider import create_provider
from athena.core.agent.runtime import Agent, create_agent
from athena.core.contracts import ArtifactStore
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import EmitEvent
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from openai import AsyncOpenAI

SchemaT = TypeVar("SchemaT", bound=BaseModel)


async def _noop_emit(
    _kind: str, _ref: str, _data: dict[str, Any] | None = None
) -> None:
    """丢弃所有事件。"""


def _validate(prompt: str, model: str, config: AgentConfig) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    for name, value in (
        ("max_turns", config.max_turns),
        ("max_tokens", config.max_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if (
        isinstance(config.temperature, bool)
        or not isinstance(config.temperature, (int, float))
        or not 0 <= config.temperature <= 2
    ):
        raise ValueError("temperature must be between 0 and 2")


def _final_text(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if not isinstance(message, ModelResponse):
            continue
        if any(part.part_kind == "tool-call" for part in message.parts):
            continue
        text = "".join(
            part.content
            for part in message.parts
            if part.part_kind == "text" and isinstance(part.content, str)
        )
        if text.strip():
            return text
    raise RuntimeError("single-turn chat produced no final assistant text")


async def single_turn_chat(
    prompt: str,
    *,
    model: str,
    memory: ContextManager | None = None,
    tools: ToolRegistry | None = None,
    system_prompt: str | None = None,
    client: "AsyncOpenAI | None" = None,
    config: AgentConfig | None = None,
    emit: EmitEvent | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    """Run one Agent turn and return its final text.

    Omitting ``memory`` makes the call stateless. Supplying a ``ContextManager``
    lets the caller retain this turn and reuse it in later calls. Callers must not
    use the same mutable memory concurrently. Sampling options travel together
    in ``config``; omitted config retains the chat default temperature of 0.1.
    """

    config = config or AgentConfig(name="single-turn-chat", temperature=0.1)
    _validate(prompt, model, config)
    active_cancel = cancel if cancel is not None else asyncio.Event()
    if active_cancel.is_set():
        raise asyncio.CancelledError

    active_memory = memory if memory is not None else ContextManager()
    active_tools = tools if tools is not None else ToolRegistry()
    start_index = active_memory.snapshot()[0]
    agent = create_agent(
        model=model,
        tools=active_tools,
        system_prompt=system_prompt or "",
        client=client,
        config=config,
    )
    context = _chat_context(prompt, active_tools, "single-turn")
    context.emit = emit if emit is not None else _noop_emit
    context.cancel = active_cancel
    context.memory = active_memory

    await agent.run(context)

    return _final_text(active_memory.items[start_index:])


async def single_turn_structured_chat(
    prompt: str,
    schema: type[SchemaT],
    *,
    model: str,
    artifacts: ArtifactStore,
    client: "AsyncOpenAI | None" = None,
    tools: ToolRegistry | None = None,
) -> SchemaT:
    """Run one Agent turn, persist its validated JSON, and return the schema instance."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    active_tools = tools if tools is not None else ToolRegistry()
    agent = Agent(
        create_provider(model, client=client),
        active_tools,
        "",
        AgentConfig(name="idea-generation-structured-chat"),
        output_type=schema,
        artifacts=artifacts,
    )
    outcome = await agent.run(_chat_context(prompt, active_tools, "structured-chat"))
    return schema.model_validate_json(await artifacts.get_text(outcome.result_ref))


def _chat_context(prompt: str, tools: ToolRegistry, namespace: str) -> AgentContext:
    """Create isolated turn identities while treating the prompt as literal text."""
    identity = f"{namespace}:{uuid4().hex}"
    thread_id = f"thread:{identity}"
    return AgentContext(
        thread=AthenaThread(
            thread_id=thread_id,
            session_id=f"session:{identity}",
            status="running",
            context_ref=f"context:{identity}",
        ),
        turn=AthenaTurn(
            turn_id=f"turn:{identity}",
            thread_id=thread_id,
            request_ref=prompt,
            status="running",
        ),
        emit=_noop_emit,
        tools=tools,
        cancel=asyncio.Event(),
        input_text=prompt,
    )

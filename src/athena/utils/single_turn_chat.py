"""One-off Agent interaction with optional caller-owned conversation memory."""

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic_ai.messages import ModelMessage, ModelResponse

from athena.core.agent import AgentContext, create_agent
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import EmitEvent
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from openai import AsyncOpenAI


async def _noop_emit(
    _kind: str, _ref: str, _data: dict[str, Any] | None = None
) -> None:
    """丢弃所有事件。"""


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
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        raise ValueError("max_tokens must be a positive integer")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
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
    max_turns: int = 20,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    emit: EmitEvent | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    """Run one Agent turn and return its final text.

    Omitting ``memory`` makes the call stateless. Supplying a ``ContextManager``
    lets the caller retain this turn and reuse it in later calls. Callers must not
    use the same mutable memory concurrently.
    """

    _validate(prompt, model, max_turns, max_tokens, temperature)
    active_cancel = cancel if cancel is not None else asyncio.Event()
    if active_cancel.is_set():
        raise asyncio.CancelledError

    active_memory = memory if memory is not None else ContextManager()
    active_tools = tools if tools is not None else ToolRegistry()
    start_index = active_memory.snapshot()[0]
    identity = uuid4().hex
    thread_id = f"thread:single-turn:{identity}"
    turn_id = f"turn:single-turn:{identity}"
    thread = AthenaThread(
        thread_id=thread_id,
        session_id=f"session:single-turn:{identity}",
        status="running",
        context_ref=f"context:single-turn:{identity}",
    )
    turn = AthenaTurn(
        turn_id=turn_id,
        thread_id=thread_id,
        request_ref=prompt,
        status="running",
    )
    agent = create_agent(
        model=model,
        tools=active_tools,
        system_prompt=system_prompt or "",
        client=client,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
        name="single-turn-chat",
    )
    context = AgentContext(
        thread=thread,
        turn=turn,
        emit=emit if emit is not None else _noop_emit,
        tools=active_tools,
        cancel=active_cancel,
        memory=active_memory,
        input_text=prompt,
    )

    await agent.run(context)

    return _final_text(active_memory.items[start_index:])

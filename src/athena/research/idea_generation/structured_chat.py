"""One-shot structured-output LLM call, built on this branch's Agent(output_type=...).

athena.utils.single_turn_chat.single_turn_chat (this branch's own helper) only returns final text —
it has no output_type parameter. Every LLM call in the Idea Generation pipeline needs a validated
Pydantic model back, so this module builds the same one-shot-turn pattern directly on
athena.core.agent.Agent's native output_type/artifacts support instead.
"""

import asyncio
from typing import TYPE_CHECKING, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from athena.core.agent import Agent, AgentConfig, AgentContext, create_provider
from athena.core.contracts import ArtifactStore
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry

if TYPE_CHECKING:
    from openai import AsyncOpenAI

SchemaT = TypeVar("SchemaT", bound=BaseModel)


async def _noop_emit(_kind: str, _ref: str, _data: dict | None = None) -> None:
    """Discard runtime events; single-shot structured calls have no observer."""


async def single_turn_structured_chat(
    prompt: str,
    schema: type[SchemaT],
    *,
    model: str,
    artifacts: ArtifactStore,
    client: "AsyncOpenAI | None" = None,
    tools: ToolRegistry | None = None,
) -> SchemaT:
    """Run one Agent turn and return its structured output, validated against ``schema``.

    Example:
        >>> result = await single_turn_structured_chat(
        ...     "say hi", Answer, model="gpt-4o-mini", artifacts=store)  # doctest: +SKIP
        >>> result.answer
        'hi'
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    active_tools = tools if tools is not None else ToolRegistry()
    identity = uuid4().hex
    thread_id = f"thread:structured-chat:{identity}"
    turn_id = f"turn:structured-chat:{identity}"
    thread = AthenaThread(
        thread_id=thread_id,
        session_id=f"session:structured-chat:{identity}",
        status="running",
        context_ref=f"context:structured-chat:{identity}",
    )
    turn = AthenaTurn(
        turn_id=turn_id,
        thread_id=thread_id,
        request_ref=prompt,
        status="running",
    )
    agent = Agent(
        create_provider(model, client=client),
        active_tools,
        "",
        AgentConfig(name="idea-generation-structured-chat"),
        output_type=schema,
        artifacts=artifacts,
    )
    ctx = AgentContext(
        thread=thread,
        turn=turn,
        emit=_noop_emit,
        tools=active_tools,
        cancel=asyncio.Event(),
        input_text=prompt,
    )
    outcome = await agent.run(ctx)
    return schema.model_validate_json(await artifacts.get_text(outcome.result_ref))

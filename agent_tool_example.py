"""Minimal Athena agent example — one callable tool, one streamed turn.

Run from the project root:

    uv run python agent_tool_example.py
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from athena.core.agent import AgentContext, create_agent
from athena.core.schemas import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry, tool

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_QUESTION = "请务必调用 add_numbers 工具计算 23 + 19，然后告诉我结果。"


@tool(
    name="add_numbers",
    description="Add two numbers and return their sum.",
    input_schema={
        "type": "object",
        "properties": {
            "a": {"type": "number", "description": "The first number."},
            "b": {"type": "number", "description": "The second number."},
        },
        "required": ["a", "b"],
    },
)
async def add_numbers(a: float, b: float) -> float:
    return a + b


def create_model_client(
    env_path: Path = PROJECT_ROOT / ".env",
) -> tuple[str, AsyncOpenAI]:
    """Load model settings and construct an OpenAI-compatible async client."""

    load_dotenv(env_path)
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(f"DEEPSEEK_API_KEY is missing from {env_path}")

    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return model, client


def build_agent(model: str, client: AsyncOpenAI):
    """Create the example agent with one registered tool."""

    tools = ToolRegistry()
    tools.register(add_numbers)
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=(
            "You are a concise assistant. When arithmetic is requested, "
            "always call the available add_numbers tool before answering."
        ),
        client=client,
        name="tool-example",
    )


async def run(question: str = DEFAULT_QUESTION) -> None:
    """Run one streamed turn and print tool/text events."""

    model, client = create_model_client()
    try:
        agent = build_agent(model, client)

        # emit 闭包实现 EmitEvent 协议：接收 (kind, event_ref, data)
        async def emit(kind: str, event_ref: str, data: dict | None = None) -> None:
            if kind == "agent/text_delta" and data:
                print(data.get("delta", ""), end="", flush=True)
            elif kind == "tool/begin":
                print(f"\n[tool started] {event_ref}", flush=True)
            elif kind == "tool/end":
                print(f"\n[tool completed] {event_ref}", flush=True)

        context = AgentContext(
            thread=AthenaThread(
                thread_id="example-thread",
                session_id="example-session",
                status="running",
                context_ref="context://example",
            ),
            turn=AthenaTurn(
                turn_id="example-turn",
                thread_id="example-thread",
                request_ref=question,
                status="running",
            ),
            emit=emit,
            tools=agent.config.tools,
            cancel=asyncio.Event(),
        )

        print(f"model={model} base_url={client.base_url}")
        await agent.run(context)
        print()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(run(" ".join(sys.argv[1:]) or DEFAULT_QUESTION))

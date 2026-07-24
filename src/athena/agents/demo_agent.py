"""Demo Agent — 流式 Agent Loop + 多 Agent 协作。

用法::

    uv run python -m athena.agents.demo_agent
"""

import asyncio
import os

from athena.core.agent import AgentContext, BaseAgent, create_agent
from athena.core.tool import ToolRegistry, tool

# ── 示例工具 ───────────────────────────────────────────────


@tool(
    name="read_file",
    description="Read a file from the filesystem.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
async def read_file(path: str) -> str:
    p = __import__("pathlib").Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    c = p.read_text(encoding="utf-8", errors="replace")
    return c[:5000] + "\n...[truncated]" if len(c) > 5000 else c


@tool(
    name="list_dir",
    description="List files and directories.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory path."}},
    },
)
async def list_dir(path: str = ".") -> str:
    p = __import__("pathlib").Path(path)
    if not p.is_dir():
        return f"Error: not a directory: {path}"
    items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = [f"  [{'DIR' if i.is_dir() else 'FILE'}] {i.name}" for i in items[:50]]
    return "\n".join(lines) if lines else "(empty)"


_SYS = "You are a helpful coding assistant. Keep responses concise."


def create_demo_agent(model: str = "claude-haiku-4-5-20251001"):
    reg = ToolRegistry()
    reg.register(read_file)
    reg.register(list_dir)
    return create_agent(model=model, tools=reg, system_prompt=_SYS, name="demo-agent")


class DemoAgent(BaseAgent):
    """向后兼容。"""

    name = "demo-agent"
    description = "Demo agent with tool-calling."

    def __init__(self, model: str = "claude-haiku-4-5-20251001") -> None:
        self._inner = create_demo_agent(model)

    async def run(self, ctx: AgentContext) -> tuple[str, str]:
        return await self._inner.run(ctx)


# ── 入口 ───────────────────────────────────────────────────


async def _main() -> None:
    print("=" * 60)
    print("Athena Demo Agent -- Streaming Agent Loop")
    print("=" * 60)
    agent = create_demo_agent()
    print(f"  model={agent.config.model}  tools={len(agent.config.tools.specs)}")
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        print("  API key present")
    else:
        print("  [Skip] No API key — agent ready for ThreadRuntime integration")


if __name__ == "__main__":
    asyncio.run(_main())

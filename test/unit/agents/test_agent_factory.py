from pydantic import BaseModel

from athena.agents.agent_factory import create_agent_for
from athena.core.agent.runtime import Agent
from athena.core.tool import ToolRegistry
from athena.core.agent.provider import ResponsesProvider


class _Result(BaseModel):
    ok: bool


def test_create_agent_for_builds_agent_with_output_type() -> None:
    tools = ToolRegistry()
    agent = create_agent_for(
        "report",
        model="test-model",
        tools=tools,
        system_prompt="prompt",
        output_type=_Result,
    )
    assert isinstance(agent, Agent)
    assert agent._output_type is _Result
    assert agent.model.model_name == "test-model"
    assert agent.tools is tools

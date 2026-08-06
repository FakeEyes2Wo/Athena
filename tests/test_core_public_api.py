import athena.core as core
import athena.core.agent as agent


def test_supported_public_surfaces_remain_available() -> None:
    core_names = {
        "BaseTool",
        "EmitEvent",
        "ToolContext",
        "ToolRegistry",
        "ToolResult",
        "ToolSpec",
        "Agent",
        "AgentConfig",
        "AgentContext",
        "AgentControl",
        "AgentEvent",
        "AgentOutcome",
        "BaseAgent",
        "StreamEvent",
        "ToolCall",
        "agent_runner",
        "create_agent",
    }
    agent_names = {
        "Agent",
        "AgentConfig",
        "AgentContext",
        "AgentControl",
        "AgentEvent",
        "AgentHandle",
        "AgentOutcome",
        "AgentResult",
        "BaseAgent",
        "ResponsesProvider",
        "StepOutcome",
        "StreamEvent",
        "ToolCall",
        "agent_runner",
        "create_agent",
        "create_code_agent",
    }
    assert core_names <= set(core.__all__)
    assert agent_names <= set(agent.__all__)
    for name in core_names:
        assert getattr(core, name) is not None
    for name in agent_names:
        assert getattr(agent, name) is not None

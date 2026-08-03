"""AgentRuntime 测试 — 模型解析与热切换。构造 agent 不会发起网络请求。"""

import pytest

from athena.core.agent.agent import Agent, create_agent
from athena.core.tool import ToolRegistry
from athena.tui.runner import FALLBACK_MODEL, MODEL_ENV, AgentRuntime, GatedTool


def bare_agent(model: str) -> Agent:
    """没有签名默认值的 profile 工厂 —— 用来验证兜底常量这条路径。"""
    return create_agent(model=model, tools=ToolRegistry(), system_prompt="x")


async def allow(ctx, args) -> bool:
    return True


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_env_model_wins_over_factory_default(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "qwen3.7-plus")
    assert AgentRuntime().model == "qwen3.7-plus"


def test_explicit_model_wins_over_env(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "qwen3.7-plus")
    assert AgentRuntime(model="gpt-5").model == "gpt-5"


def test_falls_back_to_factory_default():
    assert AgentRuntime().model == "claude-haiku-4-5-20251001"


def test_factory_without_defaults_uses_constant():
    runtime = AgentRuntime(profiles={"bare": bare_agent}, profile="bare")
    assert runtime.model == FALLBACK_MODEL


def test_switching_profile_keeps_env_model(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "qwen3.7-plus")
    runtime = AgentRuntime(
        profiles={"demo": bare_agent, "other": bare_agent}, profile="demo"
    )
    runtime.set_profile("other")
    assert runtime.profile == "other" and runtime.model == "qwen3.7-plus"


def test_set_model_rebuilds_agent():
    runtime = AgentRuntime()
    before = runtime._agent
    runtime.set_model("qwen3.7-plus")
    assert runtime._agent is not before
    assert runtime._agent.config.model == "qwen3.7-plus"


def test_unknown_profile_is_rejected():
    with pytest.raises(KeyError):
        AgentRuntime(profile="nope")
    with pytest.raises(KeyError):
        AgentRuntime().set_profile("nope")


def test_gate_wraps_every_tool():
    runtime = AgentRuntime(gate=allow)
    tools = runtime._agent.config.tools
    assert runtime.tool_names == ["list_dir", "read_file"]
    assert all(isinstance(tools.resolve(n), GatedTool) for n in runtime.tool_names)


def test_no_gate_leaves_tools_untouched():
    runtime = AgentRuntime()
    tools = runtime._agent.config.tools
    assert not any(isinstance(tools.resolve(n), GatedTool) for n in runtime.tool_names)


def test_runner_exposes_both_signatures():
    runtime = AgentRuntime()
    assert callable(runtime)
    assert hasattr(runtime, "run_with_context")


def test_profile_names_are_sorted():
    runtime = AgentRuntime(profiles={"z": bare_agent, "a": bare_agent}, profile="a")
    assert runtime.profile_names == ["a", "z"]

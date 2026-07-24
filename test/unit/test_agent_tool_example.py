import pytest
from openai import AsyncOpenAI

import agent_tool_example as example


def test_create_model_client_reads_deepseek_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=test-key\n"
        "DEEPSEEK_MODEL=test-model\n"
        "DEEPSEEK_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    model, client = example.create_model_client(env_path)

    assert model == "test-model"
    assert client.api_key == "test-key"
    assert str(client.base_url) == "https://example.invalid"


def test_build_agent_registers_add_numbers_tool():
    client = AsyncOpenAI(api_key="test-key")
    agent = example.build_agent("test-model", client)

    assert "add_numbers" in agent.config.tools
    assert agent._provider.client is client


@pytest.mark.asyncio
async def test_add_numbers_tool():
    result = await example.add_numbers.execute({"a": 23, "b": 19}, None)

    assert result == 42

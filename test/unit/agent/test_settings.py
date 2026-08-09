# test/unit/agent/test_settings.py
import pytest

from athena.core.agent import settings


def test_settings_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    assert settings.base_url() == "https://api.deepseek.com"
    assert settings.model_name() == "deepseek:flash"
    assert settings.api_key() is None


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("MODEL_NAME", "deepseek-chat")
    assert settings.api_key() == "sk-test"
    assert settings.model_name() == "deepseek-chat"


def test_get_client_raises_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        settings.get_client()

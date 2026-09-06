# test/unit/agent/test_settings.py
import pytest

from athena.core.agent import settings


def test_settings_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # base_url() 现在经 provider_kind() 选默认端点，所以 LLM_PROVIDER 也参与这个断言：
    # 不清掉它，开发机 .env 里的 provider 会让"未配置时的默认值"随机器而变。
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_PRO", raising=False)
    assert settings.base_url() == "https://api.deepseek.com"
    assert settings.model_name() == "deepseek-v4-flash"
    assert settings.pro_model_name() == "deepseek-v4-pro"
    assert settings.api_key() is None


def test_default_context_window_and_max_tokens(monkeypatch):
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    assert settings.context_window() == 1_000_000
    assert settings.max_tokens() == 384_000


def test_settings_reads_env(monkeypatch):
    # ``settings`` 在导入时就 load_dotenv()，所以开发者本地的 .env 会参与解析。
    # 不清掉优先级更高的那个变量，这条用例就取决于谁的机器在跑。
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("MODEL_NAME", "deepseek-chat")
    monkeypatch.setenv("MODEL_PRO", "deepseek-reasoner")
    assert settings.api_key() == "sk-test"
    assert settings.model_name() == "deepseek-chat"
    assert settings.pro_model_name() == "deepseek-reasoner"


def test_openai_key_alone_is_enough(monkeypatch):
    """只配 OPENAI_API_KEY 是最常见的一种配置，此前它永远解析不出密钥。

    ``_resolve`` 的第二个位置参数是 config.toml 的路径元组；回退值放进那个位置会被
    当成路径逐字符展开，于是整条回退链在这一种配置下静默返回 None。
    """
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")

    assert settings.api_key() == "sk-openai"


def test_the_key_fallback_prefers_the_more_specific_variable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert settings.api_key() == "sk-deepseek"

    monkeypatch.setenv("LLM_API_KEY", "sk-explicit")
    assert settings.api_key() == "sk-explicit"


def test_get_client_raises_without_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        settings.get_client()

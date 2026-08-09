"""LLM 配置：从 .env 加载 DeepSeek API 凭据并构造 OpenAI 兼容 client。"""

import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek:flash"


def _resolve(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    return value if value else default


def api_key() -> str | None:
    """读取 DEEPSEEK_API_KEY，回退 OPENAI_API_KEY。"""
    return _resolve("DEEPSEEK_API_KEY", _resolve("OPENAI_API_KEY"))


def base_url() -> str:
    """DeepSeek OpenAI 兼容端点。"""
    return _resolve("BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL


def model_name() -> str:
    """默认 deepseek:flash（最便宜档位），可经 MODEL_NAME 覆盖。"""
    return _resolve("MODEL_NAME", DEFAULT_MODEL) or DEFAULT_MODEL


def get_client() -> AsyncOpenAI:
    """构造 OpenAI 兼容 client；无 API key 直接报错。"""
    key = api_key()
    if not key:
        raise RuntimeError(
            "Missing LLM API key: set DEEPSEEK_API_KEY or OPENAI_API_KEY in .env"
        )
    return AsyncOpenAI(api_key=key, base_url=base_url())

"""LLM 配置：优先级 系统环境变量 > .env > config.toml > 内置默认值。

密钥类配置（DEEPSEEK_API_KEY / OPENAI_API_KEY 等）只从环境变量读取，不落
``config.toml``；其余非密钥配置集中在 ``config.example.toml``，复制为
``config.toml`` 后生效。
"""

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypeVar

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PRO_MODEL = "deepseek-v4-pro"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_MAX_TOKENS = 384_000

ALLOWED_PROVIDERS = ("deepseek", "openai", "qwen")

DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}

_CONFIG_PATH = Path("config.toml")

T = TypeVar("T")


@lru_cache(maxsize=1)
def _file_config() -> dict:
    """读取 ``config.toml``；文件不存在或解析失败时返回空配置，不打断启动。"""
    if not _CONFIG_PATH.is_file():
        return {}
    try:
        raw = _CONFIG_PATH.read_bytes()
        # utf-8-sig：容忍 Windows 编辑器偶尔写入的 BOM，否则 tomllib 直接报错。
        return tomllib.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def _config_value(*path: str) -> object | None:
    """按 ``[a.b.c]`` 路径从 ``config.toml`` 取标量值；任何一层缺失返回 None。"""
    node: object = _file_config()
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if isinstance(node, (str, int, float, bool)):
        return node
    return None


def _coerce(raw: object, default: T, cast: Callable[[object], T] | None) -> T:
    if cast is None:
        return raw  # type: ignore[return-value]
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _setting(
    env_name: str,
    *,
    path: tuple[str, ...] = (),
    default: T | None = None,
    cast: Callable[[object], T] | None = None,
) -> T | None:
    """环境变量 > config.toml > default（首个非空值）。

    这是 settings 的唯一取值入口：字符串、布尔、整数、浮点都通过 ``cast``
    归一化，避免每个公共函数各写一套解析逻辑。
    """
    raw = os.environ.get(env_name)
    if raw not in (None, ""):
        if default is None:
            return _coerce(raw, None, cast)  # type: ignore[arg-type]
        return _coerce(raw, default, cast)
    if path:
        value = _config_value(*path)
        if value is not None:
            if default is None:
                return _coerce(value, None, cast)  # type: ignore[arg-type]
            return _coerce(value, default, cast)
    return default


def api_key() -> str | None:
    """读取 LLM_API_KEY，回退各家的专用变量（只从环境变量读取）。"""
    return (
        _setting("LLM_API_KEY")
        or _setting("DEEPSEEK_API_KEY")
        or _setting("OPENAI_API_KEY")
        or _setting("DASHSCOPE_API_KEY")
    )


def base_url() -> str:
    """OpenAI 兼容端点：BASE_URL > config.toml ``[llm].base_url`` > provider 默认端点。"""
    configured = _setting("BASE_URL", path=("llm", "base_url"))
    if configured:
        return str(configured)
    return DEFAULT_BASE_URLS.get(provider_kind(), DEFAULT_BASE_URL)


def model_name() -> str:
    """快/省档模型：MODEL_NAME > config.toml ``[llm].model_name`` > deepseek-v4-flash。"""
    return str(
        _setting("MODEL_NAME", path=("llm", "model_name"), default=DEFAULT_MODEL)
        or DEFAULT_MODEL
    )


def pro_model_name() -> str:
    """强/贵档模型：MODEL_PRO > config.toml ``[llm].model_pro`` > deepseek-v4-pro。"""
    return str(
        _setting("MODEL_PRO", path=("llm", "model_pro"), default=DEFAULT_PRO_MODEL)
        or DEFAULT_PRO_MODEL
    )


def provider_kind() -> str:
    """LLM 后端类型：LLM_PROVIDER > config.toml ``[llm].provider`` > deepseek。"""
    kind = str(
        _setting("LLM_PROVIDER", path=("llm", "provider"), default=DEFAULT_PROVIDER)
        or DEFAULT_PROVIDER
    )
    if kind not in ALLOWED_PROVIDERS:
        raise ValueError(
            f"unsupported LLM_PROVIDER={kind!r}; "
            f"expected one of {', '.join(ALLOWED_PROVIDERS)}"
        )
    return kind


def temperature() -> float:
    """采样温度：LLM_TEMPERATURE > config.toml ``[llm].temperature`` > 0.1。"""
    raw = _setting("LLM_TEMPERATURE", path=("llm", "temperature"))
    value = _coerce(raw, 0.1, float)
    return value if 0.0 <= value <= 2.0 else 0.1


def seed() -> int | None:
    """采样随机种子：LLM_SEED > config.toml ``[llm].seed`` > 不发送。"""
    raw = _setting("LLM_SEED", path=("llm", "seed"))
    return _coerce(raw, None, int) if raw is not None else None


def context_window() -> int:
    """模型上下文窗口：LLM_CONTEXT_WINDOW > config.toml ``[llm].context_window`` > 1_000_000。

    DeepSeek V4 Flash 的最大上下文窗口为 1M token。
    """
    return int(
        _setting(
            "LLM_CONTEXT_WINDOW",
            path=("llm", "context_window"),
            default=DEFAULT_CONTEXT_WINDOW,
            cast=int,
        )
        or DEFAULT_CONTEXT_WINDOW
    )


def max_tokens() -> int:
    """单次响应最大输出 token：LLM_MAX_TOKENS > config.toml ``[llm].max_tokens`` > 384_000。

    默认值跟随 DeepSeek V4 Flash 的可用输出能力/配置口径。仍可通过
    ``LLM_MAX_TOKENS`` 或 ``[llm].max_tokens`` 覆盖。
    """
    return int(
        _setting(
            "LLM_MAX_TOKENS",
            path=("llm", "max_tokens"),
            default=DEFAULT_MAX_TOKENS,
            cast=int,
        )
        or DEFAULT_MAX_TOKENS
    )


def enable_thinking() -> bool:
    """是否让模型走思考/推理模式：LLM_ENABLE_THINKING > ``[llm].enable_thinking`` > False。"""
    raw = _setting("LLM_ENABLE_THINKING", path=("llm", "enable_thinking"))
    return str(raw).strip().lower() in ("1", "true", "yes", "on") if raw else False


def max_retries() -> int:
    """流式响应断线最多重连次数：config.toml ``[llm.retry].max_retries``，默认 5。"""
    return int(
        _setting(
            "LLM_MAX_RETRIES",
            path=("llm", "retry", "max_retries"),
            default=5,
            cast=int,
        )
        or 5
    )


def timeout_s() -> float:
    """单次流式请求总超时（秒）：config.toml ``[llm.retry].timeout_s``，默认 300。"""
    return float(
        _setting(
            "LLM_TIMEOUT_S",
            path=("llm", "retry", "timeout_s"),
            default=300.0,
            cast=float,
        )
        or 300.0
    )


def get_client() -> AsyncOpenAI:
    """构造 OpenAI 兼容 client；无 API key 直接报错。

    ``timeout``/``max_retries`` 交给 openai SDK：内置指数退避 + jitter，且尊重
    服务端 retry-after；429/5xx/连接断线属可重试，鉴权/合同错误不重试。
    """
    key = api_key()
    if not key:
        raise RuntimeError(
            "Missing LLM API key: set LLM_API_KEY, or the provider's own variable "
            "(DEEPSEEK_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY) in .env"
        )
    return AsyncOpenAI(
        api_key=key,
        base_url=base_url(),
        timeout=timeout_s(),
        max_retries=max_retries(),
    )

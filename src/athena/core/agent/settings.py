"""LLM 配置：优先级 系统环境变量 > .env > config.toml > 内置默认值。

密钥类配置（DEEPSEEK_API_KEY / OPENAI_API_KEY 等）只从环境变量读取，不落
``config.toml``；其余非密钥配置集中在 ``config.example.toml``，复制为
``config.toml`` 后生效。
"""

import os
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PRO_MODEL = "deepseek-v4-pro"
DEFAULT_PROVIDER = "deepseek"

ALLOWED_PROVIDERS = ("deepseek", "openai", "qwen")

DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}

_CONFIG_PATH = Path("config.toml")


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


def _resolve(key: str, config_path: tuple[str, ...] = (), default: str | None = None) -> str | None:
    """环境变量 > config.toml > default（首个非空值）。"""
    value = os.environ.get(key)
    if value:
        return value
    if config_path:
        configured = _config_value(*config_path)
        if isinstance(configured, bool):
            return str(configured).lower()
        if isinstance(configured, (str, int, float)):
            return str(configured)
    return default


def api_key() -> str | None:
    """读取 LLM_API_KEY，回退 DEEPSEEK_API_KEY / OPENAI_API_KEY（只从环境变量读取）。

    三层回退都必须走 ``default=`` 关键字：``_resolve`` 的第二个位置参数是 config.toml
    的路径元组，把回退值放进去会让它被当成路径逐字符展开，于是"只配了
    ``OPENAI_API_KEY``"这一种（也是最常见的一种）配置永远解析不出密钥。
    """
    return _resolve(
        "LLM_API_KEY",
        default=_resolve(
            "DEEPSEEK_API_KEY", default=_resolve("OPENAI_API_KEY")
        ),
    )


def base_url() -> str:
    """OpenAI 兼容端点：BASE_URL > config.toml ``[llm].base_url`` > provider 默认端点。"""
    configured = _resolve("BASE_URL", ("llm", "base_url"))
    if configured:
        return configured
    return DEFAULT_BASE_URLS.get(provider_kind(), DEFAULT_BASE_URL)


def model_name() -> str:
    """快/省档模型：MODEL_NAME > config.toml ``[llm].model_name`` > deepseek-v4-flash。"""
    return _resolve("MODEL_NAME", ("llm", "model_name"), DEFAULT_MODEL) or DEFAULT_MODEL


def pro_model_name() -> str:
    """强/贵档模型：MODEL_PRO > config.toml ``[llm].model_pro`` > deepseek-v4-pro。"""
    return _resolve("MODEL_PRO", ("llm", "model_pro"), DEFAULT_PRO_MODEL) or DEFAULT_PRO_MODEL


def provider_kind() -> str:
    """LLM 后端类型：LLM_PROVIDER > config.toml ``[llm].provider`` > deepseek。

    仅显式选择，不做 base_url/model 前缀推断；非法值在构造期抛 ``ValueError``。
    """
    kind = _resolve("LLM_PROVIDER", ("llm", "provider"), DEFAULT_PROVIDER)
    if kind not in ALLOWED_PROVIDERS:
        raise ValueError(
            f"unsupported LLM_PROVIDER={kind!r}; "
            f"expected one of {', '.join(ALLOWED_PROVIDERS)}"
        )
    return kind


def _int_config(path: tuple[str, ...], default: int) -> int:
    value = _config_value(*path)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _float_config(path: tuple[str, ...], default: float) -> float:
    value = _config_value(*path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def max_retries() -> int:
    """流式响应断线最多重连次数：config.toml ``[llm.retry].max_retries``，默认 5。"""
    return _int_config(("llm", "retry", "max_retries"), 5)


def timeout_s() -> float:
    """单次流式请求总超时（秒）：config.toml ``[llm.retry].timeout_s``，默认 300。"""
    return _float_config(("llm", "retry", "timeout_s"), 300.0)


def get_client() -> AsyncOpenAI:
    """构造 OpenAI 兼容 client；无 API key 直接报错。

    ``timeout``/``max_retries`` 交给 openai SDK：内置指数退避 + jitter，且尊重
    服务端 retry-after；429/5xx/连接断线属可重试，鉴权/合同错误不重试。
    """
    key = api_key()
    if not key:
        raise RuntimeError(
            "Missing LLM API key: set DEEPSEEK_API_KEY or OPENAI_API_KEY in .env"
        )
    return AsyncOpenAI(
        api_key=key,
        base_url=base_url(),
        timeout=timeout_s(),
        max_retries=max_retries(),
    )

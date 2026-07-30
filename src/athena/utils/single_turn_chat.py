"""不保存对话历史的单轮模型交互（异步）。

每次调用无状态：仅将系统提示词和用户提示词发送给模型并返回文本响应。
不创建 Thread、不写入上下文状态、不承担工作流调度。
调用方负责 Prompt（英文）、工具权限、重试、审计和中文报告后处理。

使用与项目其他部分一致的 openai.AsyncOpenAI 客户端（DeepSeek / OpenAI-compatible API）。
"""

import os

from openai import AsyncOpenAI


def _build_client() -> AsyncOpenAI:
    """按环境变量构建 OpenAI-compatible 客户端。

    ``OPENAI_API_KEY`` 为必选；``OPENAI_BASE_URL`` 可选（默认指向 DeepSeek）。
    """
    return AsyncOpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )


# 模块级共享客户端，延迟初始化
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """返回共享的 OpenAI 客户端实例。"""
    global _client
    if _client is None:
        _client = _build_client()
    return _client


async def single_turn_chat(
    system_prompt: str,
    user_prompt: str,
    response_format: dict | None = None,
    *,
    model: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """向 LLM 发送单轮异步请求并返回文本响应。

    Args:
        system_prompt: 系统级指令。
        user_prompt: 用户消息 / 任务描述。
        response_format: 可选的 OpenAI 原生格式约束（如 ``{"type": "json_object"}``）。
        model: 模型名，默认使用 ``OPENAI_MODEL`` 环境变量，回退到 ``deepseek-chat``。
        max_tokens: 最大输出 token 数。

    Returns:
        模型的文本响应。
    """
    client = _get_client()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    kw: dict = {
        "model": model or os.environ.get("OPENAI_MODEL", "deepseek-chat"),
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        kw["response_format"] = response_format

    completion = await client.chat.completions.create(**kw)
    return completion.choices[0].message.content or ""

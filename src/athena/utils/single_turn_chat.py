"""不保存对话历史的单轮模型交互。

每次调用无状态：仅将系统提示词和用户提示词发送给模型并返回文本响应。
不创建 Thread、不写入上下文状态、不承担工作流调度。
调用方负责 Prompt（英文）、工具权限、重试、审计和中文报告后处理。
"""

import json
import os

import httpx

# ---------------------------------------------------------------------------
# 配置（可通过环境变量覆盖）
# ---------------------------------------------------------------------------
_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
_API_VERSION = "2023-06-01"
_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "4096"))
_TIMEOUT = float(os.environ.get("ANTHROPIC_TIMEOUT", "120.0"))


def single_turn_chat(
    system_prompt: str,
    user_prompt: str,
    response_format: dict | None = None,
) -> str:
    """向 LLM 发送单轮请求并返回文本响应。

    Args:
        system_prompt: 系统级指令。
        user_prompt: 用户消息 / 任务描述。
        response_format: 可选的格式提示（如 ``{"type": "json_object"}``）。
            设置后会在系统提示词末尾追加指令，要求模型只输出合法 JSON。

    Returns:
        模型的文本响应（当 ``response_format`` 为 ``json_object`` 时为 JSON 字符串）。
    """
    messages = [{"role": "user", "content": user_prompt}]

    headers = {
        "x-api-key": _API_KEY,
        "anthropic-version": _API_VERSION,
        "content-type": "application/json",
    }

    body: dict = {
        "model": _MODEL,
        "max_tokens": _MAX_TOKENS,
        "system": system_prompt,
        "messages": messages,
    }

    # Anthropic API 没有原生的 response_format 参数，通过系统提示词引导模型。
    if response_format and response_format.get("type") == "json_object":
        body["system"] = (
            system_prompt
            + "\n\nYou MUST respond with valid JSON only, no other text."
        )

    url = f"{_BASE_URL}/v1/messages"

    response = httpx.post(
        url,
        headers=headers,
        json=body,
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    # Anthropic Messages API 返回的 content 是一个 block 列表
    content_blocks = payload.get("content", [])
    text_parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            text_parts.append(block["text"])

    return "".join(text_parts)

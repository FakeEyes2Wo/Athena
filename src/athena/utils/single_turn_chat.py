"""Athena 无历史单轮 LLM 调用工具。

函数每次只接收当前请求及必要配置，返回本轮结构化结果；不创建 Thread、不写入上下文状态、
不承担重试或工作流调度——这些由调用方负责（例如 IdeaGenerator 自己决定要不要重试）。
"""

import os
from typing import TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model


# ====== 常量 ======

# 默认模型："provider:model" 前缀形式，见 pydantic-ai 的 infer_model 约定；用 openai-chat
# 前缀(走 Chat Completions API，不是更新的 Responses API)，这样才能兼容 DashScope 等只实现了
# Chat Completions 形态的第三方 OpenAI 兼容端点
DEFAULT_MODEL: str = "openai-chat:gpt-4o-mini"
MODEL_ENV_VAR: str = "ATHENA_LLM_MODEL"


# ====== 类型 ======

T = TypeVar("T", bound=BaseModel)


# ====== 功能代码 ======

async def single_turn_chat(prompt: str, output_schema: type[T], *, model: Model | str | None = None) -> T:
    """对 prompt 发起一次单轮结构化调用，返回 output_schema 校验后的实例。

    Example:
        >>> class Answer(BaseModel):
        ...     value: str
        >>> await single_turn_chat("say hi", Answer)  # doctest: +SKIP
        Answer(value='hi')
    """
    resolved_model = model or os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL)
    agent = Agent(output_type=output_schema)
    result = await agent.run(prompt, model=resolved_model)
    return result.output

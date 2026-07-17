"""Athena 无历史单轮 LLM 调用工具。

函数每次只接收当前请求及必要配置，返回本轮结构化结果；不创建 Thread、不写入上下文状态、
不承担重试或工作流调度——这些由调用方负责（例如 IdeaGenerator 自己决定要不要重试）。
"""

import os
from typing import Protocol, TypeVar

from pydantic import BaseModel


# ====== 常量 ======

# 默认模型："provider:model" 前缀形式，见 langchain init_chat_model 的约定
DEFAULT_MODEL: str = "anthropic:claude-sonnet-5"
MODEL_ENV_VAR: str = "ATHENA_LLM_MODEL"


# ====== 类型 ======

T = TypeVar("T", bound=BaseModel)


class StructuredChatModel(Protocol):
    """single_turn_chat 依赖的最小模型契约：真实 LangChain 模型与测试替身都满足它。"""

    def with_structured_output(self, schema: type[BaseModel]) -> object: ...


# ====== 功能代码 ======

async def single_turn_chat(
    prompt: str, output_schema: type[T], *, model: StructuredChatModel | None = None
) -> T:
    """对 prompt 发起一次单轮结构化调用，返回 output_schema 校验后的实例。

    Example:
        >>> class Answer(BaseModel):
        ...     value: str
        >>> await single_turn_chat("say hi", Answer)  # doctest: +SKIP
        Answer(value='hi')
    """
    if model is None:
        # 延迟导入：不需要真实 LLM 的调用方（例如只用 FakeChatModel 的单测）不必安装 provider 包
        from langchain.chat_models import init_chat_model

        model_name = os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL)
        model = init_chat_model(model_name)
    structured = model.with_structured_output(output_schema)
    return await structured.ainvoke(prompt)

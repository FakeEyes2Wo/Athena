"""共享测试替身：用 pydantic-ai 的 FunctionModel 脚本化确定性响应，不依赖真实 provider。

FunctionModel 是 pydantic-ai 官方提供的测试用 Model 实现（不是我们自己臆造的协议）：
把一个普通函数注入进去，函数收到消息历史和 AgentInfo，返回一个 ModelResponse；结构化输出
通过 info.output_tools[0] 对应的工具调用表达，pydantic-ai 自己负责按 output_type 校验解析。
"""

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def make_scripted_model(responses: list) -> FunctionModel:
    """按调用顺序返回预设结果的 FunctionModel：列表项可以是 BaseModel 实例、原始 dict，
    或者一个 Exception（会被原样抛出，用于模拟 provider/解析失败）。

    Example:
        >>> import asyncio
        >>> from pydantic_ai import Agent
        >>> class Answer(BaseModel):
        ...     value: str
        >>> model = make_scripted_model([Answer(value="hi")])
        >>> agent = Agent(output_type=Answer)
        >>> asyncio.run(agent.run("prompt", model=model)).output  # doctest: +SKIP
        Answer(value='hi')
    """
    queue = list(responses)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not queue:
            raise AssertionError("scripted model ran out of responses")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        args = item.model_dump(mode="json") if isinstance(item, BaseModel) else item
        tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

    return FunctionModel(respond)

"""共享测试替身：用 pydantic-ai 的 FunctionModel 脚本化确定性响应，不依赖真实 provider。

FunctionModel 是 pydantic-ai 官方提供的测试用 Model 实现（不是我们自己臆造的协议）：
把一个普通函数注入进去，函数收到消息历史和 AgentInfo，返回一个 ModelResponse；结构化输出
通过 info.output_tools[0] 对应的工具调用表达，pydantic-ai 自己负责按 output_type 校验解析。
"""

from typing import Any

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def tool_call_response(item: BaseModel | dict, info: AgentInfo) -> ModelResponse:
    """把一个 BaseModel 实例或原始 dict 包成 FunctionModel 期望的 ModelResponse：工具名取
    info.output_tools[0]（没有则退回 "final_result"），item 序列化后装进一次 ToolCallPart。

    make_scripted_model / make_routed_model 与需要"先失败再成功"的一次性 FunctionModel
    （例如重试测试里手写计数器闭包的场景）共用这段拼装逻辑——提出来是为了不让第三份拷贝
    出现，不改变任何一处的行为。

    Example:
        >>> import asyncio
        >>> from pydantic_ai import Agent
        >>> class Answer(BaseModel):
        ...     value: str
        >>> def respond(messages, info):
        ...     return tool_call_response(Answer(value="hi"), info)
        >>> agent = Agent(output_type=Answer)
        >>> asyncio.run(agent.run("prompt", model=FunctionModel(respond))).output  # doctest: +SKIP
        Answer(value='hi')
    """
    args = item.model_dump(mode="json") if isinstance(item, BaseModel) else item
    tool_name = info.output_tools[0].name if info.output_tools else "final_result"
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])


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
        return tool_call_response(item, info)

    return FunctionModel(respond)


def make_routed_model(routes: dict[str, Any]) -> FunctionModel:
    """按 prompt 中出现的子串路由的 FunctionModel：命中数必须恰好为 1，否则抛 AssertionError。

    与 make_scripted_model 的关键区别：**不消费**响应——同一个 key 可以被并发的多个候选
    重复命中，这正是多候选并发测试需要的语义。不做最长匹配：最长的 key 不等于最贴切的 key，
    静默选一个会违背"避免静默返回错误响应"的初衷。

    Example:
        >>> import asyncio
        >>> from pydantic_ai import Agent
        >>> class Answer(BaseModel):
        ...     value: str
        >>> model = make_routed_model({"say hi": Answer(value="hi")})
        >>> agent = Agent(output_type=Answer)
        >>> asyncio.run(agent.run("say hi", model=model)).output  # doctest: +SKIP
        Answer(value='hi')
    """
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = str(messages)
        hits = [key for key in routes if key in prompt]
        if len(hits) != 1:
            raise AssertionError(
                f"routed model expects exactly one matching key, got {hits}; "
                f"prompt starts with: {prompt[:200]}"
            )
        item = routes[hits[0]]
        if isinstance(item, Exception):
            raise item
        return tool_call_response(item, info)

    return FunctionModel(respond)

"""共享测试替身：满足 single_turn_chat 所需的最小模型契约，不依赖真实 LangChain provider。

LangChain 自带的 FakeListChatModel/GenericFakeChatModel 不支持 with_structured_output（会直接
抛 NotImplementedError，因为它们没实现 bind_tools），所以这里手写一个只满足我们实际用到的
两个方法的替身。
"""

from pydantic import BaseModel


class _FakeStructuredRunnable:
    """.with_structured_output(schema) 的返回值替身；与 FakeChatModel 共享同一个结果队列。"""

    def __init__(self, results: list) -> None:
        self._results = results  # 有意共享引用而非拷贝，这样跨多次 with_structured_output 调用也能按顺序消费

    async def ainvoke(self, prompt: str) -> BaseModel:
        if not self._results:
            raise AssertionError("FakeChatModel ran out of scripted responses")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeChatModel:
    """single_turn_chat 的测试替身：按调用顺序返回预设的 Pydantic 实例，或抛出预设异常。

    Example:
        >>> import asyncio
        >>> class Answer(BaseModel):
        ...     value: str
        >>> model = FakeChatModel([Answer(value="hi")])
        >>> asyncio.run(model.with_structured_output(Answer).ainvoke("prompt"))
        Answer(value='hi')
    """

    def __init__(self, results: list) -> None:
        self._results = list(results)

    def with_structured_output(self, schema: type[BaseModel]) -> _FakeStructuredRunnable:
        return _FakeStructuredRunnable(self._results)

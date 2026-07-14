"""Agent 工具调用的 capability 注册与异步分发。

这是唯一的工具路由入口：每个 capability 绑定一个适配器，输入和输出均为 artifact
引用。该类不负责跨工具工作流、线程创建、优先级或策略审批，以保持工具边界简单。
"""

from athena.core.schemas import ArtifactRef
from athena.tool_router.contracts import ToolAdapter


class ToolRouter:
    """按 capability 注册、解析并调用工具适配器。"""

    def __init__(self) -> None:
        self._adapters: dict[str, ToolAdapter] = {}

    def register(self, capability: str, adapter: ToolAdapter) -> None:
        """注册唯一 capability；重复名称必须显式拒绝，防止工具语义被覆盖。"""
        if not capability:
            raise ValueError("Capability must not be empty.")
        if capability in self._adapters:
            raise ValueError(f"Capability is already registered: {capability}")
        self._adapters[capability] = adapter

    def resolve(self, capability: str) -> ToolAdapter:
        """解析 capability 对应的适配器；未知能力应交由调用方显式处理。"""
        try:
            return self._adapters[capability]
        except KeyError as error:
            raise KeyError(f"No adapter is registered for capability: {capability}") from error

    async def invoke(self, capability: str, request_ref: ArtifactRef) -> ArtifactRef:
        """将请求 artifact 分发给已注册适配器，并异步返回结果引用。"""
        return await self.resolve(capability).invoke(request_ref)

    @property
    def capabilities(self) -> tuple[str, ...]:
        """按注册顺序暴露可用能力，供 Agent 在受控工具集合中选择。"""
        return tuple(self._adapters)

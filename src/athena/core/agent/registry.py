"""AgentTypeRegistry — ``agent_type`` 到实例能力的静态注册表（设计 §4.1）。

注册表保存 ``agent_type -> factory``；factory 每次按 ``agent_id`` 创建全新
runtime binding（AgentSpec），保证同类型多个实例拥有独立状态。factory 是进程
配置，不写入 GraphStore；首版只注册静态类型，动态创建新类型不在首版合同内。
"""

from typing import Callable

from athena.core.agent.types import (
    AgentCommandError,
    AgentId,
    AgentSpec,
    ErrorCode,
)

AgentFactory = Callable[[AgentId, str | None], AgentSpec]


class AgentTypeRegistry:
    """``agent_type -> factory`` 的注册表。"""

    def __init__(self) -> None:
        self._factories: dict[str, AgentFactory] = {}

    def register(self, agent_type: str, factory: AgentFactory) -> None:
        """注册一个 agent_type 的工厂；重复注册同一类型报错。"""
        if agent_type in self._factories:
            raise ValueError(f"agent_type already registered: {agent_type}")
        self._factories[agent_type] = factory

    def contains(self, agent_type: str) -> bool:
        """agent_type 是否已注册。"""
        return agent_type in self._factories

    def require_spec(self, agent_type: str, *, agent_id: AgentId) -> AgentSpec:
        """按 agent_type 创建该实例的全新 binding；未注册抛 NOT_FOUND。

        factory 返回带独立 runner 状态的 spec，同类型多实例不复用同一对象。
        """
        factory = self._factories.get(agent_type)
        if factory is None:
            raise AgentCommandError(
                ErrorCode.NOT_FOUND, f"unknown agent_type: {agent_type}"
            )
        return factory(agent_id, None)

    @property
    def types(self) -> tuple[str, ...]:
        """已注册类型（排序后），供 Supervisor turn 注入列表。"""
        return tuple(sorted(self._factories))

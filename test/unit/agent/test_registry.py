"""AgentTypeRegistry 单元测试（设计 §4.1）：每实例 factory 绑定。"""

import pytest

from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentCommandError, AgentSpec, ErrorCode

from ._support import EchoRunner, JsonCodec


class _CountingRunner:
    """每次实例化独立计数的 runner，验证 factory 每 agent_id 调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, request, *, session, emit) -> dict:
        self.calls.append(request)
        return {"echo": request}


def _factory() -> callable:
    return lambda _aid: AgentSpec(runner=EchoRunner(), codec=JsonCodec())


def test_register_and_require_spec_round_trip() -> None:
    registry = AgentTypeRegistry()
    registry.register("data", _factory())
    assert registry.require_spec("data", agent_id="a1") is not None
    assert registry.types == ("data",)


def test_duplicate_register_rejected() -> None:
    registry = AgentTypeRegistry()
    registry.register("data", _factory())
    with pytest.raises(ValueError):
        registry.register("data", _factory())


def test_unregistered_type_raises_not_found() -> None:
    registry = AgentTypeRegistry()
    with pytest.raises(AgentCommandError) as raised:
        registry.require_spec("missing", agent_id="a1")
    assert raised.value.code == ErrorCode.NOT_FOUND


def test_types_are_sorted() -> None:
    registry = AgentTypeRegistry()
    registry.register("plot", _factory())
    registry.register("data", _factory())
    assert registry.types == ("data", "plot")


def test_factory_creates_independent_binding_per_agent_id() -> None:
    """设计 gap #7：同类型多个 agent_id 拥有独立 runner 状态，不共享可变对象。"""
    registry = AgentTypeRegistry()
    registry.register(
        "echo",
        lambda _aid: AgentSpec(runner=_CountingRunner(), codec=JsonCodec()),
    )
    first = registry.require_spec("echo", agent_id="a1").runner
    second = registry.require_spec("echo", agent_id="a2").runner
    assert first is not second  # 每实例全新 binding
    assert first.calls == [] and second.calls == []

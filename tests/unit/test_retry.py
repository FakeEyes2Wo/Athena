"""Codex 风格技术重试测试：瞬时错误分类 + retry_async + LLM 流自动重连。"""

import asyncio

import pytest

import athena.core.agent.runtime as runtime_mod
from athena.core.agent.models import AgentContext
from athena.core.agent.provider import StreamEvent
from athena.core.agent.runtime import Agent
from athena.core.retry import is_transient_error, retry_async
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.memory.context_manager import ContextManager


def test_is_transient_error_classifies() -> None:
    """白名单瞬时错误判真；业务/鉴权错误判假。"""
    assert is_transient_error(TimeoutError("timed out")) is True
    assert is_transient_error("APITimeoutError: Request timed out") is True
    assert is_transient_error("openai.RateLimitError: Error code: 429") is True
    assert is_transient_error("InternalServerError: Error code: 503") is True
    assert is_transient_error("database is locked") is True
    assert is_transient_error("connection reset") is True
    assert is_transient_error(ValueError("contract violation")) is False
    assert is_transient_error("AuthError: invalid api key") is False


async def test_retry_async_retries_transient_then_succeeds() -> None:
    """瞬时错误指数退避重试；最后一次成功后返回结果。"""
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("timed out")
        return "ok"

    result = await retry_async(flaky, attempts=4, base_delay=0)
    assert result == "ok"
    assert calls == 3


async def test_retry_async_raises_on_non_transient() -> None:
    """非瞬时错误首次即抛出，不重试。"""
    calls = 0

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("contract violation")

    with pytest.raises(ValueError, match="contract violation"):
        await retry_async(bad, attempts=4, base_delay=0)
    assert calls == 1


async def test_retry_async_gives_up_after_exhaustion() -> None:
    """瞬时错误耗尽重试次数后原样抛出最后一次异常。"""

    async def always() -> None:
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        await retry_async(always, attempts=2, base_delay=0)


# ---- LLM 流自动重连（_sampling_loop）----


class _FlakyProvider:
    """前 ``fail_times`` 次 stream 触发瞬时/非瞬时错误，之后正常输出。"""

    def __init__(self, *, fail_times: int = 1, transient: bool = True) -> None:
        self.model_name = "flaky"
        self.fail_times = fail_times
        self.transient = transient
        self.stream_calls = 0

    async def stream(self, config, tools, messages, cancel, *, output_type=None):
        self.stream_calls += 1
        if self.stream_calls <= self.fail_times:
            msg = (
                "APITimeoutError: Request timed out"
                if self.transient
                else "AuthError: invalid api key"
            )
            yield StreamEvent(kind="error", data={"message": msg})
            return
        yield StreamEvent(
            kind="text_delta", data={"delta": "done", "accumulated": "done"}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


async def _noop_emit(kind: str, ref: str, data: dict | None = None) -> None:
    pass


def _ctx() -> AgentContext:
    return AgentContext(
        thread=AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="c1"
        ),
        turn=AthenaTurn(
            turn_id="tu1", thread_id="t1", request_ref="c1", status="running"
        ),
        emit=_noop_emit,
        tools=ToolRegistry(),
        cancel=asyncio.Event(),
        memory=ContextManager(),
        input_text="do something",
    )


async def test_agent_retries_transient_stream_error(monkeypatch) -> None:
    """瞬时流错误自动重连：首次失败 → 重连成功，run 完成。"""
    monkeypatch.setattr(runtime_mod, "_RETRY_BASE_DELAY", 0)
    provider = _FlakyProvider(fail_times=1, transient=True)
    agent = Agent(provider, ToolRegistry(), "sys")
    outcome = await agent.run(_ctx())
    assert outcome.result_ref.startswith("result://")
    assert provider.stream_calls == 2  # 失败一次 + 重连成功


async def test_agent_does_not_retry_non_transient_stream_error(monkeypatch) -> None:
    """非瞬时流错误不重连，run 原样失败。"""
    monkeypatch.setattr(runtime_mod, "_RETRY_BASE_DELAY", 0)
    provider = _FlakyProvider(fail_times=1, transient=False)
    agent = Agent(provider, ToolRegistry(), "sys")
    with pytest.raises(RuntimeError, match="AuthError"):
        await agent.run(_ctx())
    assert provider.stream_calls == 1

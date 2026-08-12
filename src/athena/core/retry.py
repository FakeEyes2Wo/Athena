"""Codex 风格技术重试：瞬时基础设施错误白名单 + 指数退避 + jitter。

supervisor_design §6.1：普通可重试请求最多重试 4 次，LLM 响应流断线最多重连
5 次；仅对明确白名单中的 timeout、connection reset、HTTP 429/5xx、SQLite busy
和临时 I/O 错误生效。鉴权失败、合同校验失败、确定性脚本异常和资源上限错误
不做技术重试。技术重试不消费研究预算。
"""

import asyncio
import random
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")

# 瞬时错误类型白名单（isinstance）。
_TRANSIENT_EXC_TYPES: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    BrokenPipeError,
    sqlite3.OperationalError,  # "database is locked" / "database is busy"
)

# 瞬时错误消息特征（大小写不敏感；覆盖 openai/httpx/requests 封装后的文本与
# Agent 外层包装的 ``{type}: {msg}``）。
_TRANSIENT_TOKENS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection error",
    "connection dropped",
    "remote protocol",
    "broken pipe",
    "rate limit",
    "too many requests",
    "error code: 429",
    "error code: 5",
    "status 429",
    "status 5",
    "5xx",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "api connection error",
    "api timeout",
    "apiconnectionerror",
    "apitimeouterror",
    "ratelimiterror",
    "database is locked",
    "database is busy",
    "operationalerror",
)


def is_transient_error(exc_or_msg: BaseException | str) -> bool:
    """判定异常（或异常文本）是否属于可自动重试的瞬时基础设施错误。

    按类型白名单 → HTTP 状态码（429/5xx）→ 消息特征三层次匹配；均不命中则
    视为业务/确定性错误，不做技术重试。
    """
    if isinstance(exc_or_msg, BaseException):
        if isinstance(exc_or_msg, _TRANSIENT_EXC_TYPES):
            return True
        status = getattr(exc_or_msg, "status_code", None)
        if isinstance(status, int) and (status == 429 or status >= 500):
            return True
        text = f"{type(exc_or_msg).__name__}: {exc_or_msg}"
    else:
        text = str(exc_or_msg)
    low = text.lower()
    return any(token in low for token in _TRANSIENT_TOKENS)


async def retry_async(
    fn: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    classify: Callable[[BaseException], bool] = is_transient_error,
) -> _T:
    """指数退避 + jitter 重试；仅对 ``classify`` 判定为瞬时的错误重试。

    最后一次尝试仍失败时原样抛出；非瞬时错误在首次即抛出。技术重试不消费
    研究预算（由调用方保证）。
    """
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:
            if attempt == attempts - 1 or not classify(exc):
                raise
            delay = min(max_delay, base_delay * (2**attempt)) * (0.5 + random.random())
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # 循环内必然 return 或 raise

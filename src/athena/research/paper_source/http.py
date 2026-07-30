"""带主机级限流的最小异步 HTTP GET 层。

只用标准库 ``urllib``：arXiv 服务条款要求"同时只保持一个连接、每 3 秒不超过 1 次请求"，
在这个节奏下连接复用与 HTTP/2 没有收益，因此不为取源引入新依赖。所有非 2xx 都作为
``HttpResponse`` 正常返回，重试与退避集中在 ``HostRateLimiter`` 一处，调用方不需要区分
"异常"与"错误状态码"两条路径。

请求一律声明 ``Accept-Encoding: identity``：``arxiv.org/src`` 的 payload 本身就是 gzip
压缩的 tar，再叠一层传输压缩会让落盘字节与服务端字节不一致，破坏内容寻址的可复现性。
"""

import asyncio
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_TIMEOUT = 60.0
MAX_RESPONSE_BYTES = 256 * 1024 * 1024
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
ARXIV_MIN_INTERVAL = 3.0
DEFAULT_BUCKET_INTERVALS = {
    "arxiv.org": ARXIV_MIN_INTERVAL,
    "openalex.org": 0.2,
}


def rate_limit_bucket(host: str) -> str:
    """把主机名收敛成限流桶名；``export.arxiv.org`` 与 ``arxiv.org`` 共用一份配额。

    arXiv 的"每 3 秒 1 次请求"约束的是整个服务，而元数据、OAI-PMH 和 e-print 分别位于
    三个子域。按主机名分桶会让实际速率变成限额的三倍，恰好是取源阶段最容易触发的情形。
    """
    lowered = host.lower().split(":")[0]
    for bucket in DEFAULT_BUCKET_INTERVALS:
        if lowered == bucket or lowered.endswith(f".{bucket}"):
            return bucket
    return lowered


class HttpTransportError(OSError):
    """连接、DNS、TLS 或读取超时等传输层失败（不含 HTTP 错误状态码）。"""


@dataclass(slots=True)
class HttpResponse:
    """一次 GET 的完整结果；头部键统一小写。"""

    status: int
    url: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """状态码是否落在 2xx。"""
        return 200 <= self.status < 300

    def header(self, name: str) -> str:
        """大小写无关地取头部值，缺失时返回空串。"""
        return self.headers.get(name.lower(), "")


class HttpTransport(Protocol):
    """可替换的 GET 传输契约，单元测试用假实现避免触网。"""

    async def get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        """发起一次 GET；HTTP 错误状态码通过返回值表达而不是抛异常。"""


def lower_headers(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    """把响应头折叠成小写键字典，便于大小写无关取值。"""
    return {str(key).lower(): str(value) for key, value in items}


def build_user_agent(contact: str | None = None) -> str:
    """构造带联系方式的 User-Agent；arXiv 与 OpenAlex 都以此识别礼貌客户端。"""
    suffix = f"; mailto={contact}" if contact else ""
    return f"AthenaPaperSource/0.1 (+https://github.com/athena-ai4s/athena{suffix})"


class UrllibTransport:
    """``urllib.request`` 的线程池包装。

    ``urlopen`` 是阻塞调用，放进 ``asyncio.to_thread`` 后不会挡住事件循环；重定向由
    urllib 自动跟随，因此 ``/e-print`` → ``/src`` 的 301 无需调用方处理。
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes

    def get_sync(self, url: str, headers: dict[str, str]) -> HttpResponse:
        """同步发起 GET；供 ``asyncio.to_thread`` 调用，也便于直接测试。"""
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read(self._max_bytes + 1)
                if len(body) > self._max_bytes:
                    raise HttpTransportError(
                        f"GET {url} exceeded the {self._max_bytes} byte limit."
                    )
                return HttpResponse(
                    status=response.status,
                    url=response.geturl(),
                    body=body,
                    headers=lower_headers(response.headers.items()),
                )
        except urllib.error.HTTPError as error:
            # 服务端返回 4xx/5xx → 交给限流层统一决定重试或放弃
            return HttpResponse(
                status=error.code,
                url=url,
                body=error.read(),
                headers=lower_headers(error.headers.items()),
            )
        except urllib.error.URLError as error:
            # DNS / TLS / 连接被拒 → 转为可重试的传输异常
            raise HttpTransportError(f"GET {url} failed: {error.reason}") from error
        except TimeoutError as error:
            # 读取阶段超时（socket.timeout）→ 同样按可重试处理
            raise HttpTransportError(f"GET {url} timed out.") from error

    async def get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        """在线程中执行阻塞 GET。"""
        return await asyncio.to_thread(self.get_sync, url, headers)


class HostRateLimiter:
    """按服务分桶串行化 GET，并强制最小请求间隔与 429/5xx 退避。

    限流粒度是服务而不是全局，也不是单个主机名：OpenAlex 每秒 10 次的宽松额度不该被
    arXiv 的 3 秒节奏拖慢，而 arXiv 的三个子域必须共用同一份配额。每个桶一把
    ``asyncio.Lock``，同时满足 arXiv 的"单连接"要求。``request_count`` 暴露真实请求数，
    供批量取源统计成本。
    """

    def __init__(
        self,
        transport: HttpTransport | None = None,
        default_interval: float = ARXIV_MIN_INTERVAL,
        bucket_intervals: dict[str, float] | None = None,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        max_backoff: float = 60.0,
        contact_email: str | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._transport = transport or UrllibTransport()
        self._default_interval = default_interval
        self._bucket_intervals = dict(DEFAULT_BUCKET_INTERVALS)
        if bucket_intervals:
            self._bucket_intervals.update(bucket_intervals)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._user_agent = build_user_agent(contact_email)
        self._sleeper = sleeper or asyncio.sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}
        self.request_count = 0

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        """按服务节奏发起一次 GET；429/5xx 退避重试后仍失败则原样返回最后一次响应。"""
        bucket = rate_limit_bucket(urllib.parse.urlsplit(url).netloc)
        merged = {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "identity",
        }
        if headers:
            merged.update(headers)
        lock = self._locks.setdefault(bucket, asyncio.Lock())
        async with lock:
            return await self._get_locked(bucket, url, merged)

    async def _get_locked(
        self, bucket: str, url: str, headers: dict[str, str]
    ) -> HttpResponse:
        """在已持有服务锁的前提下发起请求，并对可重试状态码退避重试。"""
        attempt = 0
        while True:
            await self._wait_for_slot(bucket)
            self.request_count += 1
            response = await self._transport.get(url, headers)
            if response.status not in RETRY_STATUS or attempt >= self._max_retries:
                return response
            await self._sleeper(self._retry_delay(response, attempt))
            attempt += 1

    async def _wait_for_slot(self, bucket: str) -> None:
        """等到该服务的最小间隔到期，并预订下一个请求槽位。"""
        interval = self._bucket_intervals.get(bucket, self._default_interval)
        now = time.monotonic()
        earliest = self._next_allowed.get(bucket, 0.0)
        self._next_allowed[bucket] = max(now, earliest) + interval
        if earliest > now:
            await self._sleeper(earliest - now)

    def _retry_delay(self, response: HttpResponse, attempt: int) -> float:
        """优先采用 ``Retry-After``，否则指数退避并加抖动避免多主机同步重试。"""
        retry_after = response.header("retry-after").strip()
        if retry_after.isdigit():
            return min(float(retry_after), self._max_backoff)
        jittered = self._backoff_base * (2**attempt) * (1.0 + random.random())
        return min(jittered, self._max_backoff)

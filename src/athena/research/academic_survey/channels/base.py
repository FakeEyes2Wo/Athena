"""真实论文检索通道共享的 HTTP 边界。"""

import asyncio
import json

from athena.research.paper_source.http import HostRateLimiter, HttpResponse
from athena.storage.artifact_store import ArtifactStore


class ChannelFailure(RuntimeError):
    """图层可据此区分可重试与终止失败。"""

    retryable = False


class RetryableChannelFailure(ChannelFailure):
    retryable = True


class TerminalChannelFailure(ChannelFailure):
    pass


class HttpChannel:
    """只封装取消、状态分类、JSON 解析与原始响应持久化。"""

    name: str
    version: str

    def __init__(self, http: HostRateLimiter, artifacts: ArtifactStore) -> None:
        self.http = http
        self.artifacts = artifacts

    async def get(
        self,
        url: str,
        cancel: asyncio.Event,
        headers: dict[str, str] | None = None,
    ) -> tuple[HttpResponse, str]:
        self._check_cancel(cancel)
        try:
            response = await self.http.get(url, headers)
        except OSError as error:
            raise RetryableChannelFailure(
                f"{self.name} transport failed: {error}"
            ) from error
        self._check_cancel(cancel)
        raw_ref = await self.artifacts.put_bytes(response.body)
        if response.ok:
            return response, raw_ref
        message = f"{self.name} returned HTTP {response.status}"
        if response.status == 429 or response.status >= 500:
            raise RetryableChannelFailure(message)
        raise TerminalChannelFailure(message)

    async def get_json(
        self,
        url: str,
        cancel: asyncio.Event,
        headers: dict[str, str] | None = None,
    ) -> tuple[object, str]:
        response, raw_ref = await self.get(url, cancel, headers)
        try:
            return json.loads(response.body), raw_ref
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RetryableChannelFailure(
                f"{self.name} returned invalid JSON"
            ) from error

    @staticmethod
    def _check_cancel(cancel: asyncio.Event) -> None:
        if cancel.is_set():
            raise asyncio.CancelledError


def text(value: object) -> str:
    return " ".join(str(value or "").split())


def year_from(value: object) -> int | None:
    try:
        year = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 9999 else None

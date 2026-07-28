"""AcademicSurvey 的内容引用缓存与 exact replay 通道。"""

import asyncio
import hashlib
import json
import threading
import time
from contextvars import ContextVar, Token
from typing import Protocol

from pydantic import BaseModel

from athena.research.academic_survey.interfaces import ChannelAdapter, SurveyChains
from athena.research.academic_survey.schemas import (
    CandidateObservation,
    JudgmentDraft,
    QueryEvolution,
    QueryPlan,
    RewrittenQuery,
    SearchPage,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
    SurveyRequest,
)
from athena.storage.artifact_store import ArtifactStore

_RUN_METRICS: ContextVar[dict[str, int] | None] = ContextVar(
    "academic_survey_run_metrics", default=None
)


def start_run_metrics() -> tuple[dict[str, int], Token]:
    """为一个并发安全的 Agent run 建立独立计数器。"""
    metrics: dict[str, int] = {}
    return metrics, _RUN_METRICS.set(metrics)


def reset_run_metrics(token: Token) -> None:
    _RUN_METRICS.reset(token)


def record_run_metric(name: str, value: int = 1) -> None:
    metrics = _RUN_METRICS.get()
    if metrics is not None:
        metrics[name] = metrics.get(name, 0) + value


class CacheRecord(BaseModel):
    key: str
    value_ref: str
    created_at: float
    status: str = "success"


class SurveyCache(Protocol):
    async def get(self, key: str) -> CacheRecord | None: ...

    async def put(self, record: CacheRecord) -> None: ...


class MemorySurveyCache:
    """Agent 生命周期内跨 Turn 共享的并发安全缓存索引。"""

    def __init__(self) -> None:
        self._records: dict[str, CacheRecord] = {}
        self._lock = threading.Lock()

    async def get(self, key: str) -> CacheRecord | None:
        with self._lock:
            return self._records.get(key)

    async def put(self, record: CacheRecord) -> None:
        with self._lock:
            self._records[record.key] = record


class ReplayCacheMiss(LookupError):
    """exact replay 请求了采集快照中不存在的查询。"""


def channel_cache_key(
    channel: str,
    adapter_version: str,
    operation: str,
    payload: dict,
    limit: int,
    cursor: str = "",
) -> str:
    canonical = _canonical(
        {
            "channel": channel,
            "adapter_version": adapter_version,
            "operation": operation,
            "payload": payload,
            "cursor": cursor,
            "limit": limit,
        }
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class CachedChannelAdapter:
    """为任意真实通道增加幂等响应缓存。"""

    def __init__(
        self,
        delegate: ChannelAdapter,
        artifacts: ArtifactStore,
        cache: SurveyCache,
        *,
        name: str | None = None,
        max_age_seconds: float | None = None,
    ) -> None:
        self.delegate = delegate
        self.name = name or delegate.name
        self.version = getattr(delegate, "version", "unversioned")
        self.artifacts = artifacts
        self.cache = cache
        self.max_age_seconds = max_age_seconds
        self.cache_hits = 0
        self.network_calls = 0
        self._locks: dict[str, asyncio.Lock] = {}

    async def search(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        key = channel_cache_key(
            self.name,
            self.version,
            "search",
            {
                "query": " ".join(query.text.casefold().split()),
                "constraints": _normalized_constraints(constraints),
            },
            limit,
        )
        return await self._get_or_call(
            key, lambda: self.delegate.search(query, constraints, limit, cancel)
        )

    async def search_page(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cursor: str | None,
        cancel: asyncio.Event,
    ) -> SearchPage:
        normalized_cursor = cursor or ""
        key = channel_cache_key(
            self.name,
            self.version,
            "search_page",
            {
                "query": " ".join(query.text.casefold().split()),
                "constraints": _normalized_constraints(constraints),
            },
            limit,
            normalized_cursor,
        )

        async def call() -> SearchPage:
            method = getattr(self.delegate, "search_page", None)
            if method is not None:
                return await method(query, constraints, limit, cursor, cancel)
            return SearchPage(
                observations=await self.delegate.search(
                    query, constraints, limit, cancel
                )
            )

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self.cache.get(key)
            if cached and self._fresh(cached):
                self.cache_hits += 1
                record_run_metric("cache_hits")
                return SearchPage.model_validate_json(
                    await self.artifacts.get_text(cached.value_ref)
                )
            self.network_calls += 1
            record_run_metric("cache_misses")
            page = await call()
            value_ref = await self.artifacts.put_text(page.model_dump_json())
            await self.cache.put(
                CacheRecord(key=key, value_ref=value_ref, created_at=time.time())
            )
            return page

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        key = channel_cache_key(
            self.name,
            self.version,
            "references",
            {"identity": paper.identity.model_dump(mode="json")},
            limit,
        )
        return await self._get_or_call(
            key, lambda: self.delegate.references(paper, limit, cancel)
        )

    async def _get_or_call(self, key: str, call) -> list[CandidateObservation]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self.cache.get(key)
            if cached and self._fresh(cached):
                self.cache_hits += 1
                record_run_metric("cache_hits")
                return await _load_observations(self.artifacts, cached.value_ref)
            self.network_calls += 1
            record_run_metric("cache_misses")
            observations = await call()
            value_ref = await _put_observations(self.artifacts, observations)
            await self.cache.put(
                CacheRecord(key=key, value_ref=value_ref, created_at=time.time())
            )
            return observations

    def _fresh(self, record: CacheRecord) -> bool:
        return self.max_age_seconds is None or (
            time.time() - record.created_at <= self.max_age_seconds
        )


class ReplayChannelAdapter:
    """只读缓存的通道；miss 必须令 GEPA rollout 无效。"""

    def __init__(
        self,
        name: str,
        version: str,
        artifacts: ArtifactStore,
        cache: SurveyCache,
    ) -> None:
        self.name = name
        self.version = version
        self.artifacts = artifacts
        self.cache = cache

    async def search(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        key = channel_cache_key(
            self.name,
            self.version,
            "search",
            {
                "query": " ".join(query.text.casefold().split()),
                "constraints": _normalized_constraints(constraints),
            },
            limit,
        )
        return await self._require(key)

    async def search_page(
        self,
        query: SearchQuery,
        constraints: SurveyConstraints,
        limit: int,
        cursor: str | None,
        cancel: asyncio.Event,
    ) -> SearchPage:
        key = channel_cache_key(
            self.name,
            self.version,
            "search_page",
            {
                "query": " ".join(query.text.casefold().split()),
                "constraints": _normalized_constraints(constraints),
            },
            limit,
            cursor or "",
        )
        record = await self.cache.get(key)
        if record is None:
            raise ReplayCacheMiss(f"exact replay cache miss: {self.name}:{key}")
        return SearchPage.model_validate_json(
            await self.artifacts.get_text(record.value_ref)
        )

    async def references(
        self,
        paper: SurveyCandidate,
        limit: int,
        cancel: asyncio.Event,
    ) -> list[CandidateObservation]:
        key = channel_cache_key(
            self.name,
            self.version,
            "references",
            {"identity": paper.identity.model_dump(mode="json")},
            limit,
        )
        return await self._require(key)

    async def _require(self, key: str) -> list[CandidateObservation]:
        record = await self.cache.get(key)
        if record is None:
            raise ReplayCacheMiss(f"exact replay cache miss: {self.name}:{key}")
        return await _load_observations(self.artifacts, record.value_ref)


class CachedSurveyChains:
    """按模型 revision、bundle version、schema 与输入缓存四个 LLM 模块。"""

    def __init__(
        self,
        delegate: SurveyChains,
        artifacts: ArtifactStore,
        cache: SurveyCache,
    ) -> None:
        self.delegate = delegate
        self.artifacts = artifacts
        self.cache = cache
        self.prompt_bundle_version = delegate.prompt_bundle_version
        self.model_revision = getattr(delegate, "model_revision", "unknown")
        self.prompt_bundle = getattr(delegate, "prompt_bundle", None)
        self.cache_hits = 0
        self.calls = 0

    @property
    def token_usage(self) -> int:
        return getattr(self.delegate, "token_usage", 0)

    async def understand(self, request: SurveyRequest) -> QueryPlan:
        return await self._cached(
            "understand",
            request.model_dump(mode="json"),
            QueryPlan,
            self.delegate.understand,
            request,
        )

    async def rewrite(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        query: SearchQuery,
        channel: str,
    ) -> RewrittenQuery:
        method = getattr(self.delegate, "rewrite", None)
        if method is None:
            return RewrittenQuery(text=query.text)
        return await self._cached(
            "rewrite",
            {
                "request": request.model_dump(mode="json"),
                "domain": plan.domain,
                "query": query.model_dump(mode="json"),
                "channel": channel,
            },
            RewrittenQuery,
            method,
            request,
            plan,
            query,
            channel,
        )

    async def judge(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        candidate: SurveyCandidate,
    ) -> JudgmentDraft:
        return await self._cached(
            "judge",
            {
                "request": request.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json"),
            },
            JudgmentDraft,
            self.delegate.judge,
            request,
            plan,
            candidate,
        )

    async def evolve(
        self,
        request: SurveyRequest,
        plan: QueryPlan,
        accepted: list[SurveyCandidate],
        searched_queries: list[str],
    ) -> QueryEvolution:
        return await self._cached(
            "evolve",
            {
                "request": request.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "accepted": [item.model_dump(mode="json") for item in accepted[:10]],
                "searched_queries": searched_queries,
            },
            QueryEvolution,
            self.delegate.evolve,
            request,
            plan,
            accepted,
            searched_queries,
        )

    async def _cached(self, module: str, payload: dict, schema, call, *args):
        key = hashlib.sha256(
            _canonical(
                {
                    "module": module,
                    "model_revision": self.model_revision,
                    "prompt_bundle_version": self.prompt_bundle_version,
                    "output_schema": schema.__name__ + ":1",
                    "input": payload,
                }
            ).encode()
        ).hexdigest()
        record = await self.cache.get(key)
        if record:
            self.cache_hits += 1
            record_run_metric("cache_hits")
            return schema.model_validate_json(
                await self.artifacts.get_text(record.value_ref)
            )
        self.calls += 1
        record_run_metric("llm_calls")
        value = await call(*args)
        value_ref = await self.artifacts.put_text(value.model_dump_json())
        await self.cache.put(
            CacheRecord(key=key, value_ref=value_ref, created_at=time.time())
        )
        return value


async def _load_observations(
    artifacts: ArtifactStore, ref: str
) -> list[CandidateObservation]:
    return [
        CandidateObservation.model_validate(item)
        for item in json.loads(await artifacts.get_text(ref))
    ]


async def _put_observations(
    artifacts: ArtifactStore, observations: list[CandidateObservation]
) -> str:
    return await artifacts.put_text(
        json.dumps([item.model_dump(mode="json") for item in observations])
    )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_constraints(constraints: SurveyConstraints) -> dict:
    value = constraints.model_dump(mode="json")
    for field in ("venues", "languages", "domains"):
        value[field] = sorted(
            " ".join(item.casefold().split()) for item in value[field]
        )
    return value

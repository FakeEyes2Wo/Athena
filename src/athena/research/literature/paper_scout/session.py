"""一次 PaperScout 运行的共享状态与两个动作的执行逻辑。

工具是薄壳，真正的状态转移在这里：搜到或扩展出的论文先打分，再按 τ 过滤，最后并入
paper pool。把它单独放一层是因为两个动作共享同一段"打分 → 过滤 → 入池 → 算过程奖励"
的尾部逻辑，而 Athena 的工具对象本身不持有跨调用状态。

论文允许一步内并行发多个工具调用，因此 pool 会被同一事件循环里的多个任务同时读写。
入池是 read-modify-write，必须整体在锁内完成；打分是慢调用，放在锁外。
"""

import asyncio
import time
from dataclasses import dataclass, field

from athena.research.literature.paper_scout.backends import (
    ReferenceBackend,
    SearchBackend,
)
from athena.research.literature.paper_scout.pool import (
    PaperPool,
    locator_for,
    title_key,
)
from athena.research.literature.paper_scout.schemas import (
    ACCEPT_THRESHOLD,
    EXPAND_COST,
    REPEAT_PENALTY,
    REWARD_THRESHOLD,
    REWARD_TOP_K,
    SEARCH_COST,
    ScoutAction,
    ScoutPaper,
    ScoutRequest,
)
from athena.research.literature.paper_scout.scorer import (
    RelevanceReranker,
    RelevanceScorer,
)


def process_reward(scores: list[float], cost: float) -> float:
    """复现论文的过程奖励：top-k 命中数减去调用成本。

    ``process_reward([0.9, 0.5, 0.1], 0.1)`` 返回 ``1.9``——0.9 与 0.5 都不低于 0.4 各计
    1 分，0.1 不计，再扣掉一次调用的 0.1 成本。
    """
    top = sorted(scores, reverse=True)[:REWARD_TOP_K]
    return sum(1.0 for score in top if score >= REWARD_THRESHOLD) - cost


@dataclass(frozen=True, slots=True)
class ScoutServices:
    """Search and ranking providers shared by a scout run."""

    search_backends: list[SearchBackend]
    reference_backend: ReferenceBackend | None
    scorer: RelevanceScorer
    reranker: RelevanceReranker | None = None


@dataclass(slots=True)
class ScoutRunState:
    """Mutable pool, trace, counters, and lock for one scout run."""

    pool: PaperPool = field(default_factory=PaperPool)
    history: list[tuple[str, str]] = field(default_factory=list)
    actions: list[ScoutAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    step: int = 0
    backend_seconds: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ScoutSession:
    """paper pool、动作历史与后端的持有者。

    ``history`` 同时服务两个目的：渲染提示里的 History Actions，以及判定重复动作。论文
    对重复动作给固定负奖励而不是直接拒绝执行，这里保持一致——动作照样记入历史，但不再
    真正打一次后端。
    """

    def __init__(
        self,
        request: ScoutRequest,
        services: ScoutServices,
    ) -> None:
        self.request = request
        self.services = services
        self.state = ScoutRunState()

    async def search(self, query: str) -> ScoutAction:
        """执行一次 ``search``：跨后端检索、打分、按 τ 入池。"""
        cleaned = query.strip()
        action = ScoutAction(
            step=max(self.state.step, 1), kind="search", argument=cleaned
        )
        if not cleaned or ("search", cleaned) in self.state.history:
            action.repeated = True
            action.reward = -REPEAT_PENALTY
            self._record(action, ("search", cleaned))
            return action

        found: list[ScoutPaper] = []
        seen: set[str] = set()
        outcomes = await asyncio.gather(
            *(
                self._ask_backend(backend, cleaned)
                for backend in self.services.search_backends
            )
        )
        for backend, (results, error) in zip(
            self.services.search_backends, outcomes, strict=True
        ):
            if error is not None:
                # 单个后端失败（限流、解析失败、网络）→ 记录并继续用其他后端
                self.state.errors.append(
                    f"{backend.name}: {type(error).__name__}: {error}"
                )
                action.error = f"{backend.name}: {type(error).__name__}"
                continue
            for paper in results:
                # 同一篇论文常同时来自 arXiv 与 S2，标题键让跨后端的重复也能并掉
                keys = {paper.paper_key, title_key(paper.title)}
                if keys & seen:
                    continue
                seen |= keys
                found.append(paper)
        action.returned = len(found)
        await self._absorb(found, action, SEARCH_COST)
        self._record(action, ("search", cleaned))
        return action

    async def expand(self, locator: str) -> ScoutAction:
        """执行一次 ``expand``：沿目标论文的参考文献扩展一跳。

        接受 ``locator_for`` 给出的任意定位符，不只是 arXiv id。引用后端本来就能为
        DOI 与 S2 id 构造查询（``SemanticScholarBackend._locator``），此前卡在这一层
        把非 arXiv 的输入直接判死——而实测池里约一半是纯期刊论文。

        "认不出这个定位符"与"这篇已经扩展过"是两回事，分开记：前者是错误，后者才是
        重复动作。合在一起会让 ``repeated_actions`` 同时统计模型的重复和我们的解析
        失败，扣分也扣在错的地方。
        """
        cleaned = locator.strip()
        async with self.state.lock:
            target = self.state.pool.resolve(cleaned)
            expandable = target is not None and self.state.pool.mark_expanded(
                target.paper_key
            )
        action = ScoutAction(
            step=max(self.state.step, 1),
            kind="expand",
            argument=locator_for(target) if target is not None else cleaned,
        )
        if target is None:
            action.error = "unknown paper locator"
            self.state.errors.append(f"expand: unknown paper locator {cleaned!r}")
            self._record(action, ("expand", action.argument))
            return action
        if not expandable:
            action.repeated = True
            action.reward = -REPEAT_PENALTY
            self._record(action, ("expand", action.argument))
            return action
        if self.services.reference_backend is None:
            action.error = "no reference backend configured"
            self._record(action, ("expand", action.argument))
            return action

        started = time.monotonic()
        try:
            found = await self.services.reference_backend.references(
                target, self.request.expand_top_k
            )
        except Exception as error:  # noqa: BLE001 - external backend boundary
            self.state.backend_seconds += time.monotonic() - started
            # 引用后端失败 → 该动作零收益，但论文仍保持已扩展，避免立刻重复请求
            self.state.errors.append(
                f"{self.services.reference_backend.name}: {type(error).__name__}: {error}"
            )
            action.error = (
                f"{self.services.reference_backend.name}: {type(error).__name__}"
            )
            self._record(action, ("expand", action.argument))
            return action

        self.state.backend_seconds += time.monotonic() - started
        action.returned = len(found)
        await self._absorb(found, action, EXPAND_COST)
        self._record(action, ("expand", action.argument))
        return action

    async def _ask_backend(
        self, backend: SearchBackend, query: str
    ) -> tuple[list[ScoutPaper], Exception | None]:
        """问一个检索后端，把异常当返回值交回去而不是抛出。

        并发发出而不是挨个等：两个后端是两个不同的服务，``HostRateLimiter`` 按服务分桶、
        每桶一把锁，因此它们之间本来就没有节流上的相互作用——串行等待纯属白等。

        **结果与串行版逐字相同。** ``asyncio.gather`` 保序，调用方按同样的后端顺序做同样
        的去重，于是 ``found`` 的内容和次序都不变；异常也仍然按顺序记进 ``errors``，
        ``action.error`` 同样停在最后一个失败的后端上。异常必须当返回值传回，否则一个后端
        失败会让 ``gather`` 取消其余的——那才是真正的行为改变。

        计时仍是按后端各自累加（串行累计，见 ``ScoutStats`` 的说明）：并发之后这几项之和
        会超过 scout 墙钟，那是预期的，不是记账错误。
        """
        started = time.monotonic()
        try:
            return (
                await backend.search(
                    query, self.request.search_top_k, self.request.published_to
                ),
                None,
            )
        except Exception as error:  # noqa: BLE001 - external backend boundary
            return [], error
        finally:
            self.state.backend_seconds += time.monotonic() - started

    async def _absorb(
        self, found: list[ScoutPaper], action: ScoutAction, cost: float
    ) -> None:
        """打分并把过阈值的新论文并入池，同时算出该动作的过程奖励。

        打分与 rerank 并发：两者读同一批论文、互不依赖，而 rerank 在真机上打完 352 篇
        只要 0.4 秒——串起来发就是白等，并发发出去等于不花墙钟。

        没配 reranker 时 ``affinity`` 保持 0.0，排序退回 ``tie_break``（见 ``rank_key``）。
        """
        async with self.state.lock:
            fresh = [paper for paper in found if not self.state.pool.contains(paper)]
        if not fresh:
            return
        scores, affinities = await asyncio.gather(
            self.services.scorer.score(self.request.query, fresh),
            self._affinity(fresh),
        )
        accepted: list[float] = []
        async with self.state.lock:
            for paper, score, affinity in zip(fresh, scores, affinities, strict=True):
                if score < ACCEPT_THRESHOLD:
                    continue
                paper.relevance = score
                paper.affinity = affinity
                if not self.state.pool.add(paper):
                    continue
                accepted.append(score)
        action.accepted = len(accepted)
        action.reward = process_reward(accepted, cost)

    async def _affinity(self, papers: list[ScoutPaper]) -> list[float]:
        """取同分次序信号；没配 reranker 时返回全 0，与"没拿到信号"是同一种取值。"""
        if self.services.reranker is None:
            return [0.0] * len(papers)
        return await self.services.reranker.affinity(self.request.query, papers)

    def _record(self, action: ScoutAction, entry: tuple[str, str]) -> None:
        self.state.actions.append(action)
        self.state.history.append(entry)

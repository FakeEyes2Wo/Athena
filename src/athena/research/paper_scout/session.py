"""一次 PaperScout 运行的共享状态与两个动作的执行逻辑。

工具是薄壳，真正的状态转移在这里：搜到或扩展出的论文先打分，再按 τ 过滤，最后并入
paper pool。把它单独放一层是因为两个动作共享同一段"打分 → 过滤 → 入池 → 算过程奖励"
的尾部逻辑，而 Athena 的工具对象本身不持有跨调用状态。

论文允许一步内并行发多个工具调用，因此 pool 会被同一事件循环里的多个任务同时读写。
入池是 read-modify-write，必须整体在锁内完成；打分是慢调用，放在锁外。
"""

import asyncio

from athena.research.paper_scout.backends import ReferenceBackend, SearchBackend
from athena.research.paper_scout.schemas import (
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
from athena.research.paper_scout.pool import PaperPool, title_key
from athena.research.paper_scout.scorer import RelevanceScorer
from athena.research.paper_source.schemas import normalize_arxiv_id


def process_reward(scores: list[float], cost: float) -> float:
    """复现论文的过程奖励：top-k 命中数减去调用成本。

    ``process_reward([0.9, 0.5, 0.1], 0.1)`` 返回 ``1.9``——0.9 与 0.5 都不低于 0.4 各计
    1 分，0.1 不计，再扣掉一次调用的 0.1 成本。
    """
    top = sorted(scores, reverse=True)[:REWARD_TOP_K]
    return sum(1.0 for score in top if score >= REWARD_THRESHOLD) - cost


class ScoutSession:
    """paper pool、动作历史与后端的持有者。

    ``history`` 同时服务两个目的：渲染提示里的 History Actions，以及判定重复动作。论文
    对重复动作给固定负奖励而不是直接拒绝执行，这里保持一致——动作照样记入历史，但不再
    真正打一次后端。
    """

    def __init__(
        self,
        request: ScoutRequest,
        search_backends: list[SearchBackend],
        reference_backend: ReferenceBackend | None,
        scorer: RelevanceScorer,
    ) -> None:
        self.request = request
        self.search_backends = search_backends
        self.reference_backend = reference_backend
        self.scorer = scorer
        self.pool = PaperPool()
        self.history: list[tuple[str, str]] = []
        self.actions: list[ScoutAction] = []
        self.errors: list[str] = []
        self.step = 0
        self._lock = asyncio.Lock()

    async def search(self, query: str) -> ScoutAction:
        """执行一次 ``search``：跨后端检索、打分、按 τ 入池。"""
        cleaned = query.strip()
        action = ScoutAction(step=max(self.step, 1), kind="search", argument=cleaned)
        if not cleaned or ("search", cleaned) in self.history:
            action.repeated = True
            action.reward = -REPEAT_PENALTY
            self._record(action, ("search", cleaned))
            return action

        found: list[ScoutPaper] = []
        seen: set[str] = set()
        for backend in self.search_backends:
            try:
                results = await backend.search(
                    cleaned, self.request.search_top_k, self.request.published_to
                )
            except Exception as error:
                # 单个后端失败（限流、解析失败、网络）→ 记录并继续用其他后端
                self.errors.append(f"{backend.name}: {type(error).__name__}: {error}")
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

    async def expand(self, arxiv_id: str) -> ScoutAction:
        """执行一次 ``expand``：沿目标论文的参考文献扩展一跳。"""
        bare, _ = normalize_arxiv_id(arxiv_id)
        action = ScoutAction(
            step=max(self.step, 1), kind="expand", argument=bare or arxiv_id
        )
        key = f"arxiv:{bare}" if bare else ""
        async with self._lock:
            expandable = bool(key) and self.pool.mark_expanded(key)
            target = self.pool.get(key) if key else None
        if not expandable or target is None:
            action.repeated = True
            action.reward = -REPEAT_PENALTY
            self._record(action, ("expand", action.argument))
            return action
        if self.reference_backend is None:
            action.error = "no reference backend configured"
            self._record(action, ("expand", action.argument))
            return action

        try:
            found = await self.reference_backend.references(
                target, self.request.expand_top_k
            )
        except Exception as error:
            # 引用后端失败 → 该动作零收益，但论文仍保持已扩展，避免立刻重复请求
            self.errors.append(
                f"{self.reference_backend.name}: {type(error).__name__}: {error}"
            )
            action.error = f"{self.reference_backend.name}: {type(error).__name__}"
            self._record(action, ("expand", action.argument))
            return action

        action.returned = len(found)
        await self._absorb(found, action, EXPAND_COST)
        self._record(action, ("expand", action.argument))
        return action

    async def _absorb(
        self, found: list[ScoutPaper], action: ScoutAction, cost: float
    ) -> None:
        """打分并把过阈值的新论文并入池，同时算出该动作的过程奖励。"""
        async with self._lock:
            fresh = [paper for paper in found if not self.pool.contains(paper)]
        if not fresh:
            return
        scores = await self.scorer.score(self.request.query, fresh)
        accepted: list[float] = []
        async with self._lock:
            for paper, score in zip(fresh, scores, strict=True):
                if score < ACCEPT_THRESHOLD:
                    continue
                paper.relevance = score
                if not self.pool.add(paper):
                    continue
                accepted.append(score)
        action.accepted = len(accepted)
        action.reward = process_reward(accepted, cost)

    def _record(self, action: ScoutAction, entry: tuple[str, str]) -> None:
        self.actions.append(action)
        self.history.append(entry)

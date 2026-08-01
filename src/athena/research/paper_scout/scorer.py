"""相关性打分：决定论文能否进池、如何排序、以及是否进入最终交付集合。

论文用 pasa-7b-selector 输出 "True" token 的概率作为 ρ(p) ∈ [0,1]。这需要推理端点返回
logprobs，本仓库默认的 OpenAI 兼容端点不返回（实测 ``logprobs`` 字段缺失），因此提供两个
实现：

- ``TokenProbabilityScorer`` 复现论文口径，端点支持 logprobs 时使用；
- ``GradedRelevanceScorer`` 用论文 LLM-score 一节的 0–3 分级评分归一到 [0,1]，是没有
  logprobs 时的默认选择。二值 True/False 会让 ρ 只剩两个取值，Recall@k 排序和 0.5
  阈值都会退化，分级评分保留了排序所需的区分度。

两者都不读环境变量，客户端由调用方注入。
"""

import asyncio
import json
import math
import re
from typing import Protocol

from openai import AsyncOpenAI

from athena.research.paper_scout.prompts import (
    GRADED_SELECT_PROMPT,
    SELECT_PROMPT,
    format_papers_for_scoring,
)
from athena.research.paper_scout.schemas import ScoutPaper

GRADE_SCALE = 3.0
DEFAULT_BATCH_SIZE = 8
SCORING_ABSTRACT_CHARS = 1200
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
TRUE_TOKEN = re.compile(r"^\s*true", re.IGNORECASE)


class RelevanceScorer(Protocol):
    """把 ``(query, papers)`` 打成 [0,1] 分数的契约。"""

    model: str
    calls: int

    async def score(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """返回与 ``papers`` 等长、顺序一致的分数列表。"""


def clip_abstract(abstract: str) -> str:
    """截断摘要到打分够用的长度，避免一次批量请求塞爆上下文。"""
    return abstract[:SCORING_ABSTRACT_CHARS]


def parse_grades(content: str, count: int) -> list[float]:
    """把模型返回的 ``{"1": 3, ...}`` 解析成归一化分数；缺失项按 0 计。

    ``parse_grades('{"1": 3, "2": 0}', 2)`` 返回 ``[1.0, 0.0]``。
    """
    match = JSON_OBJECT.search(content or "")
    if match is None:
        return [0.0] * count
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        # 模型输出了近似 JSON 但不合法（尾随逗号、注释）→ 整批按 0 分处理
        return [0.0] * count
    if not isinstance(payload, dict):
        return [0.0] * count
    scores = []
    for index in range(1, count + 1):
        raw = payload.get(str(index), payload.get(index, 0))
        value = raw if isinstance(raw, (int, float)) else 0
        scores.append(min(max(float(value) / GRADE_SCALE, 0.0), 1.0))
    return scores


class GradedRelevanceScorer:
    """按 0–3 分级批量打分的 LLM scorer。

    批量而不是逐篇请求：一次 search 会带回 10 篇以上候选，逐篇调用会让打分的调用数
    压过策略本身的调用数，而分级评分对同批次比较反而更稳定。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float = 120.0,
    ) -> None:
        self.client = client
        self.model = model
        self.batch_size = batch_size
        self.timeout = timeout
        self.calls = 0

    async def score(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """对整批论文打分，内部按 ``batch_size`` 拆成并发请求。"""
        if not papers:
            return []
        batches = [
            papers[start : start + self.batch_size]
            for start in range(0, len(papers), self.batch_size)
        ]
        results = await asyncio.gather(
            *(self._score_batch(query, batch) for batch in batches)
        )
        return [score for batch in results for score in batch]

    async def _score_batch(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        prompt = GRADED_SELECT_PROMPT.format(
            user_query=query,
            papers=format_papers_for_scoring(
                [(paper.title, clip_abstract(paper.abstract)) for paper in papers]
            ),
        )
        self.calls += 1
        try:
            reply = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=self.timeout,
            )
        except Exception:
            # 打分失败不能把论文误判为高相关 → 整批按 0 分，让它们留在池外
            return [0.0] * len(papers)
        return parse_grades(reply.choices[0].message.content or "", len(papers))


class TokenProbabilityScorer:
    """复现论文口径：ρ(p) 为 selector 输出 "True" token 的概率。

    只有当推理端点在响应里返回 ``logprobs`` 时才可用；拿不到 logprobs 时退回按判定
    文本取 1.0/0.0，并在 ``degraded`` 上标记，让调用方知道排序信号已经退化成二值。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        concurrency: int = 8,
        timeout: float = 60.0,
    ) -> None:
        self.client = client
        self.model = model
        self.timeout = timeout
        self.calls = 0
        self.degraded = False
        self._limit = asyncio.Semaphore(concurrency)

    async def score(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """逐篇取 "True" 的概率。"""
        if not papers:
            return []
        return list(
            await asyncio.gather(*(self._score_one(query, paper) for paper in papers))
        )

    async def _score_one(self, query: str, paper: ScoutPaper) -> float:
        prompt = SELECT_PROMPT.format(
            user_query=query, title=paper.title, abstract=clip_abstract(paper.abstract)
        )
        async with self._limit:
            self.calls += 1
            try:
                reply = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4,
                    temperature=0,
                    logprobs=True,
                    top_logprobs=5,
                    timeout=self.timeout,
                )
            except Exception:
                # 打分失败 → 0 分，与 GradedRelevanceScorer 保持同一种保守降级
                return 0.0
        choice = reply.choices[0]
        probability = true_probability(getattr(choice, "logprobs", None))
        if probability is not None:
            return probability
        self.degraded = True
        return 1.0 if TRUE_TOKEN.match(choice.message.content or "") else 0.0


def true_probability(logprobs: object) -> float | None:
    """从 logprobs 里取首 token 为 "True" 的概率；端点未返回时为 ``None``。"""
    content = getattr(logprobs, "content", None)
    if not content:
        return None
    alternatives = getattr(content[0], "top_logprobs", None) or []
    for item in alternatives:
        if TRUE_TOKEN.match(getattr(item, "token", "")):
            return math.exp(getattr(item, "logprob", 0.0))
    return 0.0

"""同分论文之间的次序信号：交叉编码器给出的连续 affinity。

打分器是四档离散的，交付名额几乎总在某一档内部被截断——真机一轮 352 篇的池子里
**197 篇同分**，而截断线正好落在那一档。此前这 197 篇之间靠 ``pool.tie_break``
的 sha256 排序，那是**故意不带信号**的（见该函数的文档字符串），于是将近一半的交付
集合由散列决定。

本模块补的就是这一处，且只补这一处：**不覆盖打分器的任何判断，只在打分器明确表示
"这些无差别"的地方给出次序。** 打分器仍然决定谁进池、谁过交付门槛。

## 为什么是交叉编码器而不是让打分器更细或者多打几遍

三条路都在真机上量过（60 篇真实池子、8 遍独立打分、生产的 ``tie_break``，
不同遍之间的 top-N Jaccard）：

============================  =================  =================  =================
配置                          前 10 篇           前 20 篇           前 30 篇
============================  =================  =================  =================
1 遍 + sha256 拆平局          0.599 ± 0.202      0.690 ± 0.161      0.777 ± 0.073
**1 遍 + rerank 拆平局**      **0.692 ± 0.183**  **0.791 ± 0.090**  **0.896 ± 0.053**
2 遍取均值 + sha256           0.742 ± 0.076      0.730 ± 0.078      0.784 ± 0.059
2 遍取均值 + rerank           0.823 ± 0.096      0.753 ± 0.052      0.915 ± 0.029
============================  =================  =================  =================

在生产实际截断的位置（交付目标 20 篇、``max_papers`` 60）**一遍打分加 rerank 直接
赢过两遍取均值**，而后者要多付一整轮打分：真机实测 scout 631 秒 → 881 秒（+39.5%），
rerank 打完同一个 352 篇池子只要 **0.4 秒**。churn 的大头从来不是档位翻转，是平局
内部的任意排序。

"更细的分档"那条路早就否掉了（见 ``docs/survey_overhaul_ch.md`` 六点六）：加分辨率
只是把同一份抖动表达得更充分，交付稳定性反而从 0.818 掉到 0.667。

## 为什么不让它当主打分器

拿它和打分器比排序质量的那次对照里，金标是**打分器自己 4 遍均值 ≥0.45 的 28 篇**，
这局天然偏袒打分器，所以 top-20 命中 8 vs 13 **只能读成"两者不一致"，读不出谁对**。
要判谁对得有一份独立于两者的金标，目前没有。

除此之外还有一条实际的：affinity 的取值区间是 [0.0058, 0.3637]，与 ρ 完全不同的
尺度。只拿它排序不涉及标定；一旦拿它当主分数，``ACCEPT_THRESHOLD``、交付门槛 0.5
以及 ``bench.recall`` 的相关线全部要重测。

## 跨批次可比性

论文是分很多次动作陆续进池的，每次 ``_absorb`` 的批次大小都不同，因此**必须先确认
分数不依赖批次组成**，否则跨批次比较没有意义。2026-08-17 实测（``gte-rerank-v2``）：
同 5 篇探针放进 5 / 15 / 20 / 30 / 50 篇的批次、更换伴随文档（随机 vs 全高分）、
更换位置（批首 vs 批尾），最大绝对偏差 **0.000116**。这是逐对打分，不是批内归一化。

同一批连发三遍，逐篇分差均值 0.000034、最大 0.000861，top-N 排序完全一致
（Jaccard 1.000 ± 0.000）——与打分器温度 0 下仍然抖动形成对照。
"""

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Protocol

from athena.research.literature.paper_scout.schemas import ScoutPaper

DASHSCOPE_RERANK_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)

RERANK_DOCUMENT_CHARS = 1200
"""送进交叉编码器的摘要长度，与 ``scorer.SCORING_ABSTRACT_CHARS`` 对齐。

两者读的必须是同一段文本：affinity 用来给打分器认为无差别的论文排序，如果它比打分器
多看或少看一截摘要，排出来的次序回答的就不是同一个问题了。
"""

DEFAULT_RERANK_BATCH = 50
"""一次请求的文档数。

实测上限至少 500，取 50 是为了让单次失败作废的论文少一些——与 ``DEFAULT_BATCH_SIZE``
同样的取舍。352 篇分成 8 个请求并发发出，全程 0.4 秒，请求数不构成瓶颈。
"""

DEFAULT_RERANK_CONCURRENCY = 8
DEFAULT_RERANK_TIMEOUT = 60.0
RERANK_ATTEMPTS = 2
"""失败重试次数。

一次请求 0.4 秒，重试几乎不花钱，而失败的代价不对称：失败批次的 affinity 全为 0，
在各自档位里会被排到最后（见 ``affinity`` 的说明），那是一条系统性偏置。宁可多发一次。
"""


class RelevanceReranker(Protocol):
    """给同分论文排序的契约：返回与 ``papers`` 等长、顺序一致的 affinity。"""

    model: str
    calls: int
    failures: int

    async def affinity(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """返回每篇论文对 ``query`` 的连续亲和度，越大越相关。"""


def document_for(paper: ScoutPaper) -> str:
    """拼出送进交叉编码器的文档文本：标题加截断后的摘要。"""
    return f"{paper.title}\n{paper.abstract[:RERANK_DOCUMENT_CHARS]}"


def parse_scores(payload: dict, count: int) -> list[float]:
    """把 rerank 响应按 ``index`` 还原成与入参同序的分数列表；缺失项为 0。

    响应里的 ``results`` **不保证按 index 排列**——它是按分数降序返回的，所以必须按
    ``index`` 回填而不是按响应顺序 ``zip``。顺序错位不会报错，只会让每一篇论文都拿到
    别人的分数，而结果看上去完全正常。这与 ``OpenAIEmbedder`` 那里是同一类陷阱。
    """
    scores = [0.0] * count
    for item in payload.get("output", {}).get("results", []):
        index = item.get("index")
        if isinstance(index, int) and 0 <= index < count:
            scores[index] = float(item.get("relevance_score", 0.0))
    return scores


class DashScopeReranker:
    """DashScope ``text-rerank`` 交叉编码器。

    接口不是 OpenAI 兼容的那套，所以不能复用注入的 ``AsyncOpenAI`` 客户端；也没有走
    ``paper_source.http``——那一层是 GET 专用的 ``Protocol``，为一个 POST 去拓宽它会
    牵动所有检索后端。这里自带一个 ``urllib`` POST，与 ``UrllibTransport`` 同样的
    "阻塞调用放进 ``asyncio.to_thread``"写法，不引入新依赖。

    失败按整批 0 分降级，与 ``GradedRelevanceScorer`` 同一种保守处理：affinity 只是
    次序信号，拿不到它最坏退回 sha256，不该让整条链路失败。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        endpoint: str = DASHSCOPE_RERANK_ENDPOINT,
        batch_size: int = DEFAULT_RERANK_BATCH,
        concurrency: int = DEFAULT_RERANK_CONCURRENCY,
        timeout: float = DEFAULT_RERANK_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.batch_size = batch_size
        self.timeout = timeout
        self.calls = 0
        self.failures = 0
        self.seconds = 0.0
        self._limit = asyncio.Semaphore(concurrency)

    async def affinity(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """批量取 affinity；分数为 0 表示该篇没拿到信号。

        拿不到信号的论文在 ``pool.ranked`` 里会排到本档末尾（0 小于任何真实分数，实测
        最小值 0.0058）。这确实是一条偏置，但它只在请求失败时出现，且 ``failures``
        把它记了下来——相比之下让整轮调研失败要糟得多。
        """
        if not papers:
            return []
        batches = [
            papers[start : start + self.batch_size]
            for start in range(0, len(papers), self.batch_size)
        ]
        results = await asyncio.gather(
            *(self._rank_batch(query, batch) for batch in batches)
        )
        return [score for batch in results for score in batch]

    async def _rank_batch(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        documents = [document_for(paper) for paper in papers]
        async with self._limit:
            for attempt in range(RERANK_ATTEMPTS):
                self.calls += 1
                started = time.monotonic()
                try:
                    payload = await asyncio.to_thread(self._post, query, documents)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                    self.seconds += time.monotonic() - started
                    # 网络故障、超时或响应不是 JSON → 再试一次，仍失败才按 0 分降级
                    if attempt == RERANK_ATTEMPTS - 1:
                        self.failures += 1
                        return [0.0] * len(papers)
                    continue
                self.seconds += time.monotonic() - started
                return parse_scores(payload, len(papers))
        raise RuntimeError("unreachable: the retry loop either returns or degrades")

    def _post(self, query: str, documents: list[str]) -> dict:
        """同步发一次 rerank 请求；供 ``asyncio.to_thread`` 调用，也便于直接测试。"""
        body = json.dumps(
            {
                "model": self.model,
                "input": {"query": query, "documents": documents},
                "parameters": {"return_documents": False, "top_n": len(documents)},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

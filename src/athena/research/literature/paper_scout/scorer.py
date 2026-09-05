"""相关性打分：决定论文能否进池、如何排序、以及是否进入最终交付集合。

``GradedRelevanceScorer`` 用论文 LLM-score 一节的 0–3 分级评分归一到
  [0,1]。四个取值不够细，交付名额几乎总在某一档内部被截断——真机一轮 352 篇里 197 篇
  同分。本模块也包含只拆同档平局的交叉编码器；离散分数仍决定论文是否进池和交付。
实现不读环境变量，客户端由调用方注入。
"""

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from typing import Protocol

from openai import AsyncOpenAI

from athena.research.literature.paper_scout.schemas import ScoutPaper

GRADE_SCORES = (0.0, 0.2, 0.45, 1.0)

DEFAULT_BATCH_SIZE = 24
"""一次打分请求里的论文数。

延迟由输出 token 数决定，不由 prompt 大小决定：实测同一模型 8 篇要吐 857 个 token、
耗 15.8 秒，24 篇吐 1359 个、耗 23.7 秒——三倍的量只多花五成时间，每篇摊到的时间从
2.00 秒降到 1.11 秒。

批次之间是无上限 ``gather``，所以更大的批次首先省的是请求数：一个 150 篇的池从 19 个
请求降到 7 个。实测 5 路并发就已经有 33% 的排队劣化，请求少一些对墙钟同样有利。

再往上加要当心：批次越大，一次解析失败作废的论文越多（``_score_batch`` 失败时整批
按 0 分处理）。
"""

SCORING_ABSTRACT_CHARS = 1200
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
GRADED_SELECT_PROMPT = """You are an elite researcher assessing whether papers \
satisfy a research query. Judge only semantic relevance to the query; ignore writing \
quality, length, style and popularity.

Rate each paper on this four-level scale:
3 - directly answers the query; it is exactly the kind of paper being asked for
2 - clearly on topic and useful, but does not satisfy every stated condition
1 - same broad area, yet misses the specific subject of the query
0 - unrelated, or violates an explicit exclusion in the query

User Query: {user_query}

Papers:
{papers}

Return a JSON object mapping each paper index to its integer score, and nothing else.
Example for two papers: {{"1": 3, "2": 0}}"""


class RelevanceScorer(Protocol):
    """把 ``(query, papers)`` 打成 [0,1] 分数的契约。"""

    model: str
    calls: int

    async def score(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """返回与 ``papers`` 等长、顺序一致的分数列表。"""


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


def clip_abstract(abstract: str) -> str:
    """截断摘要到打分够用的长度，避免一次批量请求塞爆上下文。"""
    return abstract[:SCORING_ABSTRACT_CHARS]


def format_papers_for_scoring(papers: list[tuple[str, str]]) -> str:
    """Render scored papers with stable one-based identifiers."""
    return "\n\n".join(
        f"[{index}] Title: {title}\nAbstract: {abstract}"
        for index, (title, abstract) in enumerate(papers, start=1)
    )


def parse_grades(content: str, count: int) -> list[float]:
    """把模型返回的 ``{"1": 3, ...}`` 映射成 ρ 分数；缺失项按 0 计。

    映射不是简单的 ``grade / 3``。论文的 ρ 是 selector 判定"该论文完全满足查询"的概率，
    交付门槛 ρ ≥ 0.5；等分成三份会把 2 分（切题但不满足全部条件）放到 0.667，越过门槛，
    交付集合因此膨胀。``GRADE_SCORES`` 让 2 分落在 0.45——仍以 τ = 0.01 进池参与排序，但
    不进交付集合，只有 3 分（直接回答查询）才越过 0.5。

    ``parse_grades('{"1": 3, "2": 2}', 2)`` 返回 ``[1.0, 0.45]``。
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
        grade = min(max(int(value), 0), len(GRADE_SCORES) - 1)
        scores.append(GRADE_SCORES[grade])
    return scores


DEFAULT_PASSES = 1
"""同一批论文打几遍取均值。

**打分在温度 0 下并不确定。** 同一批 60 篇论文连打 8 遍，两遍之间的逐篇档位一致率均值
只有 0.779（区间 0.617–0.867）。这不是模型有偏——8 遍之间没有系统性漂移，109 篇跨运行
对照的均分变化是 +0.004——纯粹是抖动。端点也救不了：``seed`` 参数收下了但不兑现，同一
seed 连发三次给出三个不同答案，``system_fingerprint`` 为 ``None``（2026-08-17 实测）。

抖动会变成交付集合的 churn，取均值确实能压住它。**但它压的不是主要那一份。** 真机一轮
352 篇里 197 篇同分，截断线正好落在那一档——churn 的大头是平局内部的任意排序，不是档位
翻转。把平局交给交叉编码器之后，第二遍打分就不值那个钱了（60 篇真实池子、8 遍独立打分、
不同遍之间的 top-N Jaccard）：

============================  =================  =================  =================
配置                          前 10 篇           前 20 篇           前 30 篇
============================  =================  =================  =================
1 遍 + sha256 拆平局          0.599 ± 0.202      0.690 ± 0.161      0.777 ± 0.073
**1 遍 + rerank 拆平局**      **0.692 ± 0.183**  **0.791 ± 0.090**  **0.896 ± 0.053**
2 遍取均值 + sha256           0.742 ± 0.076      0.730 ± 0.078      0.784 ± 0.059
2 遍取均值 + rerank           0.823 ± 0.096      0.753 ± 0.052      0.915 ± 0.029
============================  =================  =================  =================

在生产实际截断的位置（交付目标 20 篇、``max_papers`` 60）一遍加 rerank 就赢过两遍取均值，
而两遍在真机上实测要多花 40% 的 scout 墙钟（631 秒 → 881 秒），rerank 打完同一个 352 篇
池子只要 0.4 秒。因此默认回到 1。

保留这个参数而不是删掉：换到没有 rerank 的部署时，调到 2 仍是当时能拿到的最好选择——
上表第三行确实优于第一行。前 10 篇那一格两遍取均值至今仍略优（0.742 vs 0.692，两者标准差
0.076/0.183 重叠），只是生产不在那个位置截断。

``passes > 1`` 时均值会产生非档位的取值（0.325 之类），那是它压平同分堆的方式；默认的
一遍不产生这些取值，同分堆改由 ``pool.rank_key`` 的 affinity 一级拆开。
"""


class GradedRelevanceScorer:
    """按 0–3 分级批量打分的 LLM scorer。

    批量而不是逐篇请求：一次 search 会带回 10 篇以上候选，逐篇调用会让打分的调用数
    压过策略本身的调用数，而分级评分对同批次比较反而更稳定。

    默认只打一遍；``passes`` 的取舍与它为什么不再需要大于 1，见 ``DEFAULT_PASSES``。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float = 120.0,
        passes: int = DEFAULT_PASSES,
    ) -> None:
        self.client = client
        self.model = model
        self.batch_size = batch_size
        self.timeout = timeout
        self.passes = max(1, passes)
        self.calls = 0
        self.seconds = 0.0

    async def score(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """对整批论文打分；打 ``passes`` 遍取均值，内部按 ``batch_size`` 拆成并发请求。"""
        if not papers:
            return []
        rounds = await asyncio.gather(
            *(self._one_pass(query, papers) for _ in range(self.passes))
        )
        if len(rounds) == 1:
            return rounds[0]
        return [sum(values) / len(values) for values in zip(*rounds)]

    async def _one_pass(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        """跑一遍完整打分。"""
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
        started = time.monotonic()
        try:
            reply = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=self.timeout,
            )
        except Exception:  # noqa: BLE001 - external scoring provider boundary
            # 打分失败不能把论文误判为高相关 → 整批按 0 分，让它们留在池外
            return [0.0] * len(papers)
        finally:
            # 失败的调用同样花了墙钟，超时那种尤其贵，不计入会低估成本
            self.seconds += time.monotonic() - started
        return parse_grades(reply.choices[0].message.content or "", len(papers))

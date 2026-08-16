"""相关性打分：决定论文能否进池、如何排序、以及是否进入最终交付集合。

论文用 pasa-7b-selector 输出 "True" token 的概率作为 ρ(p) ∈ [0,1]，是个连续分数。本仓库
拿不到那个分数，但**原因不是端点不支持 logprobs**——那句话曾经写在这里，2026-08-17 实测
证明它是错的，见下。

- ``GradedRelevanceScorer`` —— **默认实现**。用论文 LLM-score 一节的 0–3 分级评分归一到
  [0,1]。四个取值不够细（交付名额几乎总在某一档内部被截断，见 ``pool.tie_break``），但
  它是目前手上最细的那个。
- ``TokenProbabilityScorer`` —— 复现论文口径。**代码已修好，但不要启用**，理由见该类的
  文档字符串：它会让 ρ 退化成两个取值，比四档更粗。

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
TRUE_TOKEN = re.compile(r"^\s*true", re.IGNORECASE)
FALSE_TOKEN = re.compile(r"^\s*false", re.IGNORECASE)


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

    ⚠️ **不要把它设成默认打分器。** 代码是好的（2026-08-17 修掉了读错 token 的 bug，见
    ``decision_position``），但在通用 instruct 模型上它产出的 ρ 比四档**更粗**。

    2026-08-17 在阿里云百炼 compatible-mode 上逐条实测：

    1. **端点支持 logprobs。** 此前模块头写着"端点不返回（实测字段缺失）"，是错的。
       条件是必须与 ``top_logprobs`` 一起发——只发 ``logprobs=True`` 时 qwen3.6-flash
       字段缺失、qwen3.7-plus 返回 0 个候选；加上 ``top_logprobs=5`` 两个模型都给满 5 个。
       本类发的正是这个组合，所以端点从来不是障碍。
    2. **旧实现读错了 token**，于是每篇都得 0.0。已修。
    3. **修好之后 ρ 仍然只有两个取值。** 判决 token 的概率是 ``' False':1.000``，
       其余候选 ``0.000``——温度 0 下模型完全饱和。取到的 ρ 因此非 1 即 0。

    第 3 条才是它不可用的真正原因，而且换端点解决不了：论文的 ρ 来自
    ``pasa-7b-selector``，一个**为这件事微调、输出分布经过校准**的判别器；通用 instruct
    模型在二选一判断上给的就是饱和概率。要拿回连续 ρ 得换判别模型，那是训练问题不是
    接口问题。

    留着它有两个用处：换到校准过的 selector 时直接可用；以及作为"端点能力"与"模型标定"
    是两件事的记录。
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


def decision_position(content: list) -> int | None:
    """找出判决 token 在序列里的位置；找不到时返回 ``None``。

    不能假定判决在 ``content[0]``。``SELECT_PROMPT`` 要求的输出格式是
    ``Decision: True/False``，于是首 token 是 ``'Decision'``——实测
    （qwen3.6-flash，2026-08-17）：

    ======  ==============  ====================================================
    位置    token           top_logprobs
    ======  ==============  ====================================================
    0       ``'Decision'``  ``'Decision':1.000, 'Dec':0.000, 'Reason':0.000 …``
    1       ``':'``         ``':':1.000 …``
    2       ``' False'``    ``' False':1.000, ' false':0.000, ' True':0.000 …``
    ======  ==============  ====================================================

    判决在位置 2。旧实现只看位置 0，在那里找不到 ``True``，于是**每一篇都返回 0.0**——
    分数看上去正常（是个合法的 [0,1] 浮点），实际毫无信息。这是"安静地成功"的又一例。

    按 token 内容定位而不是按固定下标：换个提示词或换个模型，前缀长度就会变。
    """
    for position, item in enumerate(content):
        token = getattr(item, "token", "")
        if TRUE_TOKEN.match(token) or FALSE_TOKEN.match(token):
            return position
    return None


def true_probability(logprobs: object) -> float | None:
    """从 logprobs 里取判决 token 为 "True" 的概率；端点未返回时为 ``None``。

    ⚠️ **本函数已修好，但 ``TokenProbabilityScorer`` 仍然不该启用。** 原因不在这里，
    在模型：见该类的文档字符串。
    """
    content = getattr(logprobs, "content", None)
    if not content:
        return None
    position = decision_position(content)
    if position is None:
        return None
    alternatives = getattr(content[position], "top_logprobs", None) or []
    for item in alternatives:
        if TRUE_TOKEN.match(getattr(item, "token", "")):
            return math.exp(getattr(item, "logprob", 0.0))
    # 判决 token 找到了，但候选里没有 True——说明模型给 True 的概率低于 top_k 截断
    return 0.0

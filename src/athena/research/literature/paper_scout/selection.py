"""交付选片：把"从同分档里散列抽签"换成"按分面覆盖 + 边界档重排"。

这是整条文献链路里唯一一处**信号被随机毁掉**的地方，也是整改里性价比最高的一处。

打分是四档离散的（``GRADE_SCORES``），而交付名额几乎**总是**在某一档内部被截断。真机
实测一轮：8 篇满分直接入选，剩下 5 个名额由 **35 篇同为 0.45 的论文**争夺，胜负由
``tie_break`` 的 sha256 散列决定——那是刻意设计成"无偏"的，也就是刻意设计成**无信息**
的。后果可以直接量出来：同一查询两次跑测，交付的 10 篇里只有 2 篇相同。

下游做得再对也救不回这一步。语义检索的 known-item MRR 已经很高，找不到的东西不是没被
检索到，是它根本没进语料——任务主指标是 ROC-AUC 时语料里三篇讲 AUC 的论文一次都没被
引用，不是检索的问题，是选片没保证它们在。

## 两件事，一次模型调用

1. **边界档重排。** 只对跨过截断线的那一档做一次 listwise 重排。上面那档 35 篇，一次
   调用即可，而它决定了将近一半的交付集合。已经确定入选的那几篇不参与重排——它们的分数
   本来就更高，重排它们只会用一个更弱的信号去覆盖一个更强的。
2. **分面覆盖。** 同一次调用里把课题拆成若干面（问题类型 / 数据模态 / 指标 / 方法族），
   并标注每篇覆盖哪些面。填名额时先补没被覆盖的面，再按名次填。"十篇都很贴题但全在讲
   同一个方法"是这条链路真实发生过的失败，而按相关性排序无法表达它。

模型不可用、边界档装得下、或者解析失败时，一律回退到原来的散列次序：选片必须永远给得
出结果，重排是增益不是前提。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from openai import AsyncOpenAI

from athena.research.literature.paper_scout.pool import tie_break
from athena.research.literature.paper_scout.schemas import ScoutPaper

JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
MAX_FACETS = 5

SELECTION_ABSTRACT_CHARS = 320
"""交给重排的摘要长度。

从 700 降到 320 是被真机逼的：一次 48 篇的边界档按 700 字符渲染约 34 000 字符输入，加上
逐篇的排序输出，整次调用在 120 秒里跑不完（实测两臂都是 ``APITimeoutError``）。判"这篇
比那篇更贴题"用不到整段摘要——前 320 字符已经覆盖问题设定与方法名。
"""

MAX_RANKED = 40
"""一次最多交给模型重排几篇。

边界档可以很大——真机实测浅检索那轮是 **48 篇同为 0.20**。全部塞进去既撑大输入，又要求
模型输出 48 行排序，而延迟由输出 token 决定。超出的部分保持散列次序排在后面：它们本来
就在候选的尾部，重排与否几乎不改变交付集合。
"""

DEFAULT_TIMEOUT = 300.0
"""单次重排的超时。

120 秒是拍出来的，真机上两臂都超时，整个重排从未生效过——回退逻辑是对的，所以它安静地
退化成了散列次序，没有任何报错。300 秒配合上面两条裁剪，留出足够余量。
"""

SELECTION_PROMPT = """You are assembling the reading list for a research task. \
A relevance scorer has already run; papers above the cut are settled. Your job is the \
tier that straddles the cut, where every paper scored identically and the ranking \
below is currently arbitrary.

Research topic:
{query}

Do two things.

1. Split the topic into {max_facets} or fewer **facets** — the distinct things a \
useful reading list must cover (for example: the problem setting, the data modality, \
the target metric, and each method family worth surveying). Name each facet in three \
words or fewer. Derive them from the topic itself, not from the papers below.

2. For every paper listed, say which facets it actually addresses, and order the \
tied papers from most to least useful for this topic.

Already selected (for facet coverage only — do not rank these):
{settled}

Tied papers to rank:
{tied}

Return one JSON object and nothing else, with no prose around it:
{{"facets": ["facet name", ...],
  "settled_facets": {{"<id>": ["facet name", ...]}},
  "ranking": [{{"id": "<id>", "facets": ["facet name", ...]}}]}}

`ranking` must list every tied paper exactly once, best first. Use only facet names \
you declared in `facets`. **Write no explanations** — the ordering itself is the \
answer, and latency here is driven by output length. Judge relevance to the topic, \
not writing quality, venue or citation count."""


@dataclass(frozen=True, slots=True)
class DeliverySelection:
    """一次选片的结果与它的依据，供报告核对"这次是怎么选出来的"。"""

    delivered: list[ScoutPaper]
    facets: list[str] = field(default_factory=list)
    covered: dict[str, str] = field(default_factory=dict)
    boundary_size: int = 0
    reranked: bool = False
    note: str = ""

    def coverage(self) -> float:
        """被至少一篇交付论文覆盖的分面占比；没有分面时为 0。"""
        if not self.facets:
            return 0.0
        return len(self.covered) / len(self.facets)


class BoundarySelector(Protocol):
    """把边界档重排并标注分面的接口。"""

    async def rank(
        self, query: str, settled: list[ScoutPaper], tied: list[ScoutPaper]
    ) -> tuple[list[str], dict[str, list[str]], list[str]]:
        """返回 ``(分面列表, paper_key → 分面, 边界档的新次序)``。"""


def hash_order(papers: list[ScoutPaper]) -> list[ScoutPaper]:
    """同分档内的确定性兜底次序：稳定散列，无系统性偏置也无信息。

    保留它是因为选片必须永远给得出结果。它不再是主要排序手段——那正是要修的缺陷。
    """
    return sorted(papers, key=lambda item: tie_break(item.paper_key))


def split_at_cut(
    papers: list[ScoutPaper], limit: int
) -> tuple[list[ScoutPaper], list[ScoutPaper]]:
    """按名额把候选切成"分数严格高于截断线的"与"正好落在截断线上的"。

    ``papers`` 必须已按相关性降序排好。返回的两段之外的候选分数更低，不参与竞争。
    ``limit <= 0`` 沿用 ``PaperPool.retained`` 的口径——**不截断**，全部交付，因此没有
    截断线，也就没有要取舍的边界档。

    分档比较用的是分数本身而不是名次：打分只有四档，落在截断线上的往往有几十篇，而
    "第 10 名"这个位置在它们之间没有任何意义。
    """
    if limit <= 0 or len(papers) <= limit:
        return list(papers), []
    cut = papers[limit - 1].relevance
    certain = [item for item in papers if item.relevance > cut]
    tied = [item for item in papers if item.relevance == cut]
    return certain, tied


def fill_by_coverage(
    ordered: list[ScoutPaper],
    facets_by_paper: dict[str, list[str]],
    covered: dict[str, str],
    facets: list[str],
    slots: int,
) -> list[ScoutPaper]:
    """按"先补没覆盖的面，再按名次填"选出剩余名额。

    先补覆盖而不是直接按名次取前 N：同一档里的论文按定义分数相同，此时"再多一篇讲同一
    个方法的"远不如"第一篇讲主指标的"。补完之后仍有名额，就按重排名次顺序填，因为那时
    已经没有覆盖上的理由可以区分它们了。
    """
    chosen: list[ScoutPaper] = []
    taken: set[str] = set()
    for facet in facets:
        if len(chosen) >= slots:
            break
        if facet in covered:
            continue
        for paper in ordered:
            if paper.paper_key in taken:
                continue
            if facet in facets_by_paper.get(paper.paper_key, []):
                chosen.append(paper)
                taken.add(paper.paper_key)
                covered[facet] = paper.paper_key
                # 这一篇可能同时覆盖别的面，一并记上，免得再为它们各挑一篇
                for other in facets_by_paper.get(paper.paper_key, []):
                    covered.setdefault(other, paper.paper_key)
                break
    for paper in ordered:
        if len(chosen) >= slots:
            break
        if paper.paper_key not in taken:
            chosen.append(paper)
            taken.add(paper.paper_key)
    return chosen[:slots]


async def select_delivery(
    query: str,
    papers: list[ScoutPaper],
    limit: int,
    selector: BoundarySelector | None = None,
) -> DeliverySelection:
    """从已过门槛的候选里选出交付集合。

    ``papers`` 必须已按相关性降序、同分按 ``tie_break`` 排好（即 ``PaperPool.ranked()``
    的输出经门槛与可取源过滤之后的样子）。

    重排只在**需要取舍时**发生：边界档装得下所有剩余名额时，重排改变不了任何结果，那
    一次调用就是白花的。
    """
    if limit <= 0:
        # ``retained`` 的口径：0 表示不截断。这里必须原样传下去，否则"不设上限"会被
        # 当成"一篇都不要"——真机上 ScoutRequest.max_papers 默认就是 0。
        return DeliverySelection(delivered=list(papers), note="no delivery cap")
    certain, tied = split_at_cut(papers, limit)
    slots = limit - len(certain)
    if not tied or slots <= 0:
        return DeliverySelection(
            delivered=(certain + tied)[:limit], note="no boundary tier to resolve"
        )
    if len(tied) <= slots:
        return DeliverySelection(
            delivered=certain + tied,
            boundary_size=len(tied),
            note="boundary tier fits; nothing to choose between",
        )
    if selector is None:
        return DeliverySelection(
            delivered=certain + hash_order(tied)[:slots],
            boundary_size=len(tied),
            note="no selector configured; fell back to hash order",
        )
    # 只把前 MAX_RANKED 篇交给模型；其余保持散列次序排在后面。边界档在真机上可以到 48
    # 篇，全塞进去会让这次调用超时——而超时的表现是安静地退回散列，整个重排等于没做。
    ranked_order = hash_order(tied)
    head, tail = ranked_order[:MAX_RANKED], ranked_order[MAX_RANKED:]
    try:
        facets, facets_by_paper, order = await selector.rank(query, certain, head)
    except Exception as error:  # noqa: BLE001 - 选片必须永远给得出结果
        return DeliverySelection(
            delivered=certain + hash_order(tied)[:slots],
            boundary_size=len(tied),
            note=f"selector failed ({type(error).__name__}); fell back to hash order",
        )
    if not order:
        return DeliverySelection(
            delivered=certain + hash_order(tied)[:slots],
            boundary_size=len(tied),
            note="selector returned no ranking; fell back to hash order",
        )
    by_key = {item.paper_key: item for item in tied}
    ordered = [by_key[key] for key in order if key in by_key]
    # 模型漏掉的、以及超出 MAX_RANKED 没送进去的，都按散列次序补在末尾：没被排到不该让
    # 它彻底出局，但也不该插到已经被排过的那些前面
    seen = set(order)
    ordered.extend(item for item in head if item.paper_key not in seen)
    ordered.extend(tail)
    covered: dict[str, str] = {}
    for paper in certain:
        for facet in facets_by_paper.get(paper.paper_key, []):
            covered.setdefault(facet, paper.paper_key)
    chosen = fill_by_coverage(ordered, facets_by_paper, covered, facets, slots)
    return DeliverySelection(
        delivered=certain + chosen,
        facets=facets,
        covered=covered,
        boundary_size=len(tied),
        reranked=True,
    )


def format_papers(papers: list[ScoutPaper], *, with_abstract: bool) -> str:
    """把候选渲染成提示里的编号列表；``paper_key`` 原样给出，供模型回填。"""
    if not papers:
        return "(none)"
    lines = []
    for paper in papers:
        lines.append(f"- id: {paper.paper_key}\n  title: {paper.title}")
        if with_abstract and paper.abstract:
            lines.append(f"  abstract: {paper.abstract[:SELECTION_ABSTRACT_CHARS]}")
    return "\n".join(lines)


def parse_selection(
    content: str, tied_keys: set[str]
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """解析模型回复；任何一处不合规都退化成"没有可用结果"而不是半份结果。

    只接受**声明过的分面名**：模型偶尔会在 ``ranking`` 里发明新面，放行的话覆盖度就成了
    模型自己定义的指标，而不是对课题的覆盖。同理，``ranking`` 里不在边界档中的 id 一律
    丢弃——那要么是幻觉，要么指向本来就不参与竞争的论文。
    """
    match = JSON_OBJECT.search(content or "")
    if match is None:
        return [], {}, []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        # 近似 JSON（尾随逗号、围栏内注释）→ 当作没有结果，由调用方回退
        return [], {}, []
    if not isinstance(payload, dict):
        return [], {}, []
    facets = [
        str(item).strip()
        for item in payload.get("facets", [])
        if isinstance(item, (str, int, float)) and str(item).strip()
    ][:MAX_FACETS]
    allowed = set(facets)

    def tags(raw: object) -> list[str]:
        """Normalize a provider tag payload to unique nonblank strings."""
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip() in allowed]

    facets_by_paper: dict[str, list[str]] = {}
    settled = payload.get("settled_facets")
    if isinstance(settled, dict):
        for key, raw in settled.items():
            facets_by_paper[str(key)] = tags(raw)
    order: list[str] = []
    for entry in payload.get("ranking", []) or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id", "")).strip()
        if key not in tied_keys or key in order:
            continue
        order.append(key)
        facets_by_paper[key] = tags(entry.get("facets"))
    return facets, facets_by_paper, order


class LlmBoundarySelector:
    """一次模型调用完成"拆分面 + 标注 + 重排边界档"。

    合成一次调用而不是三次：三件事共用同一份候选正文，分开调只是把同样的上下文重发三遍。
    调用数也因此与边界档大小无关——它是每轮调研固定的一次，相对 scout 已有的几十次打分
    可以忽略。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.client = client
        self.model = model
        self.timeout = timeout
        self.calls = 0

    async def rank(
        self, query: str, settled: list[ScoutPaper], tied: list[ScoutPaper]
    ) -> tuple[list[str], dict[str, list[str]], list[str]]:
        """重排边界档并标注分面。"""
        prompt = SELECTION_PROMPT.format(
            query=query,
            max_facets=MAX_FACETS,
            settled=format_papers(settled, with_abstract=False),
            tied=format_papers(tied, with_abstract=True),
        )
        self.calls += 1
        reply = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            timeout=self.timeout,
        )
        return parse_selection(
            reply.choices[0].message.content or "",
            {item.paper_key for item in tied},
        )

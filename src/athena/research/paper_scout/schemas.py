"""PaperScout 的检索状态、动作记录与结果模型。

PaperScout 把论文检索建模成 POMDP：隐状态是累积的 paper pool，Agent 每一步只看到
pool 的一个摘要视图（observation），并选择 ``search`` 或 ``expand`` 动作。这里的模型
对应论文里的三个对象——池中的一篇论文、一次动作的结果、一次运行的统计。

相关性分数 ``relevance`` 是 [0,1] 的连续值，既决定论文能否进池（τ），也决定交付集合的
排序与截断（见 ``RETAIN_THRESHOLD``），因此它是唯一贯穿全流程的排序信号。
"""

from typing import Literal

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef
from athena.research.paper_source.schemas import PaperSourcePolicy

ACCEPT_THRESHOLD = 0.01

PASA_RETAIN_THRESHOLD = 0.5
"""PaSa 论文的交付门槛 ρ ≥ 0.5，复现检索基准时必须显式传这个值。

在 PaSa 里它只是算 Precision/Recall 时画的一条线：爬到的论文全都留在树里，没有任何
下游因此拿不到它们。本仓库一度把它用作流水线闸门——``retained`` 直接决定哪些论文会
被下载——角色变了，取值却没有重新论证。

``docs/paper_scout_reproduction_ch.md`` 里的数字对应这个取值。
"""

RETAIN_THRESHOLD = 0.0
"""交付门槛的默认值：不按分数截断，只按 ``max_papers`` 取相关性最高的若干篇。

默认服务 IdeaGeneration 而不是检索基准，两者的代价结构相反。基准里误报直接扣
Precision；这里误报的代价只是多下载一篇、多转换一次，而 ``paper_rag`` 的 chunk 级
检索根本不会把它捞出来——漏报的代价却是一个永远不会生成的假设，下游没有任何一段能
把它找回来。因此工作点偏向召回。

因为 ``GRADE_SCORES`` 是离散的（0 / 0.2 / 0.45 / 1.0），非零门槛只有三种行为，各档
之间落差极大：实测一次 AI4S 式查询的候选池里 3 分 2 篇、2 分 18 篇、1 分 80 篇，
0.5 只交付 2%。取 0 后交付集合等于整个池按相关性降序，``max_papers`` 成为唯一的量
控制——这对下游也更自然，它要的是"最相关的 N 篇"而不是"分数过线的若干篇"。

取 0 不等于"什么都收"：``ACCEPT_THRESHOLD`` 仍然把 0 分（无关）的论文挡在池外。

**非零门槛只有 0.5 那一档可以跨模型使用。** 同一批 150 篇论文的实测：

======================  ==========  ==========
门槛                    plus        flash
======================  ==========  ==========
``> 0.45``（只要 3 分）  2 篇        2 篇（同样那 2 篇）
``> 0.2``（含 2 分）     29 / 37 篇  17 篇
======================  ==========  ==========

3 分档跨模型、跨会话都完全一致，三次打分零摇摆。2 分档不是——它是"切题但不完全满足"，
判据本身模糊：**同一个模型连打两遍，2 分档就是 27 篇与 35 篇，稳定率只有 77%**，而整体
档位一致率是 94%。换模型后差一倍。因此 ``retain_threshold=0.3``（即 > 0.2）在换打分模型
后必须重测；``0.5`` 不用。
"""
OBSERVATION_EXPANDED = 10
OBSERVATION_UNEXPANDED = 10
MAX_ABSTRACT_WORDS = 400
IDLE_TURNS_BEFORE_STOP = 3
REWARD_TOP_K = 3
REWARD_THRESHOLD = 0.4
REPEAT_PENALTY = 0.5
SEARCH_COST = 0.1
EXPAND_COST = 0.05


SEARCH_TOP_K_NOTE = """检索深度为什么从 10 提到 50。

论文的 ``top_k = 10`` 是针对全 arXiv 的稠密语义索引设的；本实现走 arXiv 的词法排序与
Semantic Scholar / OpenAlex，同一个常数不成立——gold 会散落在 10–50 名之间，在进入打分
之前就被丢掉。

实测（``sparbench_000``，拿 Agent **自己发出的那 4 条查询**换深度重跑，查询本身不变）：

======  ==============
深度    该题 gold 浮现
======  ==============
10      1 / 10
50      6 / 10
100     6 / 10
======  ==============

50 处已经饱和，取 50 而不是 100——多出来的深度只多付打分的钱，不多找到论文。

**这个参数不能单独调。** 每步的打分量随它线性增长，而 ``max_seconds`` 一直是真正绑定的
那条约束：三轮真机里两轮 ``stop_reason: max_seconds``、停在第 4 步，``max_steps`` 从未生
效。只提深度不放宽墙钟，换来的是步数变少——用探索广度换掉探索深度，净效果可能为负。
两者必须一起动。
"""

class ScoutPaper(BaseModel):
    """池中的一篇论文，带有它是怎样被发现的以及它的相关性分数。"""

    paper_key: str = Field(description="Stable identity key used for pool dedup.")
    arxiv_id: str = Field(default="", description="Bare arXiv id without version.")
    doi: str = Field(default="", description="Normalized DOI when known.")
    s2_paper_id: str = Field(default="", description="Semantic Scholar paper id.")
    title: str = Field(description="Paper title.")
    abstract: str = Field(default="", description="Abstract text; may be empty.")
    year: int | None = Field(default=None, description="Publication year.")
    published_date: str = Field(default="", description="ISO date when known.")
    citation_count: int | None = Field(default=None, description="Citations if known.")
    source: Literal["search", "expand"] = Field(
        description="Which action first surfaced this paper."
    )
    origin: str = Field(
        default="",
        description="Query text for search results, parent title for expansions.",
    )
    channel: str = Field(default="", description="Backend that returned the paper.")
    open_access_pdf: str = Field(
        default="",
        description=(
            "Open-access PDF URL claimed by the retrieval channel; empty when the "
            "paper is paywalled. Unverified, and carried into paper_source as a hint."
        ),
    )
    is_open_access: bool | None = Field(
        default=None, description="Upstream open-access claim; None when unknown."
    )
    relevance: float = Field(default=0.0, ge=0.0, le=1.0, description="Score in [0,1].")
    expanded: bool = Field(
        default=False, description="Whether its references were already followed."
    )


class ScoutAction(BaseModel):
    """一次 ``search`` 或 ``expand`` 的执行结果。

    ``reward`` 复现论文的过程奖励（top-k 相关性增益减去调用成本，重复动作为负），
    在线推理不使用它，只作为可审计的过程信号记录下来。
    """

    step: int = Field(ge=1, description="1-based agent step that issued the action.")
    kind: Literal["search", "expand"] = Field(description="Action type.")
    argument: str = Field(description="Query text or paper locator.")
    returned: int = Field(default=0, ge=0, description="Raw results from the backend.")
    accepted: int = Field(
        default=0, ge=0, description="Papers newly added to the pool."
    )
    reward: float = Field(default=0.0, description="Process reward for this action.")
    repeated: bool = Field(default=False, description="Action had already been taken.")
    error: str = Field(default="", description="Backend failure, empty when fine.")


class ScoutStats(BaseModel):
    """一次运行的成本与停止原因摘要。"""

    steps: int = Field(default=0, ge=0)
    search_actions: int = Field(default=0, ge=0)
    expand_actions: int = Field(default=0, ge=0)
    repeated_actions: int = Field(default=0, ge=0)
    pool_size: int = Field(default=0, ge=0)
    scored_papers: int = Field(default=0, ge=0)
    retained_papers: int = Field(default=0, ge=0)
    dropped_no_source: int = Field(
        default=0,
        ge=0,
        description=(
            "Papers above the threshold dropped for having no arXiv source. Large "
            "values mean the query is being answered mostly by paywalled venues."
        ),
    )
    policy_calls: int = Field(default=0, ge=0)
    scorer_calls: int = Field(default=0, ge=0)
    backend_requests: int = Field(default=0, ge=0)
    boundary_tier: int = Field(
        default=0,
        ge=0,
        description=(
            "Papers tied at the score where delivery was cut. Scoring has four levels, "
            "so this is routinely dozens: it is the size of the choice that used to be "
            "made by a hash."
        ),
    )
    boundary_reranked: bool = Field(
        default=False,
        description="The boundary tier was resolved by rerank rather than by hash.",
    )
    selection_note: str = Field(
        default="",
        description=(
            "Why selection took the path it did. Kept out of `errors` on purpose: "
            "falling back to hash order is a degraded selection, not a failed run, "
            "and marking the run partial for it would hide real backend failures."
        ),
    )
    facets: list[str] = Field(
        default_factory=list,
        description="Facets the topic was split into for coverage-aware selection.",
    )
    facet_coverage: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of facets covered by the delivered set. Relevance ranking alone "
            "cannot express 'ten good papers all about the same method'."
        ),
    )
    wall_seconds: float = Field(default=0.0, ge=0.0)
    stop_reason: str = Field(default="", description="Why the loop terminated.")
    errors: list[str] = Field(default_factory=list)


class ScoutRequest(BaseModel):
    """一次 PaperScout 运行的输入。

    ``published_to`` 对应基准构造时的发布日期上限；``max_papers`` 只截断交付集合，
    不改变检索过程——但在默认的 ``retain_threshold=0`` 下它是交付量的唯一控制，
    因为交付集合此时等于整个候选池按相关性降序。
    """

    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(min_length=1, description="Natural language paper search need.")
    published_to: str = Field(
        default="", description="Inclusive ISO upper bound on publication date."
    )
    max_steps: int = Field(default=10, ge=1, description="Hard cap on agent steps.")
    max_parallel_calls: int = Field(
        default=5, ge=1, description="Tool calls honoured per step."
    )
    search_top_k: int = Field(
        default=50,
        ge=1,
        description=(
            "Results per search call. The paper's 10 does not carry over to a "
            "lexical backend; see SEARCH_TOP_K_NOTE."
        ),
    )
    expand_top_k: int = Field(
        default=20, ge=1, description="References followed per expand call."
    )
    max_papers: int = Field(default=0, ge=0, description="0 means no handoff cap.")
    max_seconds: float = Field(
        default=1800.0,
        gt=0,
        description=(
            "Wall-clock budget, raised together with search_top_k: at 600s it was "
            "the clock and not max_steps that stopped the run."
        ),
    )
    retain_threshold: float = Field(
        default=RETAIN_THRESHOLD,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum relevance for delivery; 0 delivers the whole pool ranked by "
            "relevance. Use PASA_RETAIN_THRESHOLD to reproduce the paper benchmark."
        ),
    )
    require_retrievable_source: bool = Field(
        default=True,
        description=(
            "Deliver only papers with an arXiv id or an upstream open-access PDF. "
            "Set False to reproduce retrieval benchmarks, which score identity "
            "rather than retrievability."
        ),
    )
    paper_source_policy: PaperSourcePolicy = Field(
        default_factory=PaperSourcePolicy,
        description="Fetch policy carried into the generated paper_source request.",
    )


class ScoutCorpus(BaseModel):
    """交付与审计入口：最终论文、完整池和逐动作轨迹。"""

    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(description="The request this corpus answers.")
    retained: list[ScoutPaper] = Field(
        description=(
            "Papers with relevance >= the request threshold, ranked and capped "
            "by max_papers."
        )
    )
    pool: list[ScoutPaper] = Field(description="Every paper accepted into the pool.")
    actions: list[ScoutAction] = Field(description="Ordered action trace.")
    stats_ref: ArtifactRef = Field(description="Reference to the run statistics.")


class PaperScoutResult(BaseModel):
    """Agent Turn 的顶层结果。"""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["complete", "partial"] = Field(
        description="partial when a backend failed or the budget cut the run short."
    )
    corpus_ref: ArtifactRef = Field(description="Reference to the ScoutCorpus.")
    stats_ref: ArtifactRef = Field(description="Reference to the ScoutStats.")
    paper_source_request_ref: ArtifactRef | None = Field(
        default=None,
        description=(
            "PaperSourceRequest ready for paper_fetch; None when no delivered paper "
            "carries an identifier paper_source can resolve."
        ),
    )
    paper_count: int = Field(default=0, ge=0, description="Retained paper count.")
    warnings: list[str] = Field(default_factory=list)

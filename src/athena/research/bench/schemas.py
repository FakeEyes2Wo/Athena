"""文献链路基准的输入与结果模型。

这些模型存在的理由是可 diff：每一次改动前后各跑一遍，把两份 JSON 放在一起就能看出
"哪条查询变好了、哪条变坏了"。此前这条链路的全部质量数字都来自一次性脚本，脚本本身
不在仓库里——于是任何改动都无法证明变好，任何回归也不会被发现。

因此本模块只做纯计算，不碰网络、不碰模型：``KnownItemQuery`` 是版本化的输入数据，
``RetrievalBenchReport`` / ``CorpusHealthReport`` 是版本化的输出，两端都能进 git。
"""

from typing import Literal

from pydantic import BaseModel, Field

BENCH_SCHEMA_VERSION = "1.0"


class KnownItemQuery(BaseModel):
    """一条 known-item 查询：一句改写过的提问 + 唯一正确的论文。

    ``question`` 刻意避开金标论文标题里的用词。不这样做的话，词面检索只要把标题抄回来
    就能得满分，而这条基准要测的恰恰是"提问措辞与论文措辞不一致时还能不能找到"——那是
    Ideator 的真实处境：它带着任务的说法来问，不带着论文的说法。

    ``gold`` 是论文 id 而不是 chunk id：命中哪个 chunk 无所谓，找到那篇论文才是目的。
    允许多个是因为有些问题确实有两篇同样正确的论文，此时按最靠前的那篇计名次。
    """

    query_id: str = Field(description="Stable identifier used to diff two runs.")
    question: str = Field(min_length=1, description="Paraphrased user-side question.")
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Terms handed to the keyword channel. Separate from `question` because the "
            "two channels take different input shapes; a shared string would measure "
            "the adapter, not the channel."
        ),
    )
    gold: list[str] = Field(
        min_length=1, description="Paper ids that correctly answer this question."
    )
    rationale: str = Field(
        default="", description="Why these papers and no others; read when a gold moves."
    )


class QuerySet(BaseModel):
    """一组 known-item 查询，连同它假定的语料。

    ``corpus_papers`` 记下写这组查询时语料里有哪些论文。金标不在语料里就不是"检索失败"
    而是"这道题在这份语料上无从回答"，两者必须分开报——真机上正是这一步当场抓出一个
    错标（《When AUC meets DRO》通篇用 KL 散度与 CVaR，不该作为 Wasserstein 那题的答案）。
    """

    name: str = Field(description="Query set identity, e.g. imbalance_auc.")
    description: str = Field(default="", description="What this set is for.")
    corpus_papers: int = Field(
        default=0, ge=0, description="Papers in the corpus these golds were written on."
    )
    queries: list[KnownItemQuery] = Field(min_length=1)


class QueryOutcome(BaseModel):
    """单条查询在单个通道上的结果。"""

    query_id: str
    rank: int | None = Field(
        default=None,
        description="1-based rank of the first gold paper; null when not retrieved.",
    )
    returned_papers: list[str] = Field(
        default_factory=list, description="Distinct paper ids in returned order."
    )
    error: str = Field(default="", description="Why this query could not be scored.")


class ChannelScore(BaseModel):
    """一个检索通道在整组查询上的成绩。

    同时报 hit@k 与 MRR：hit@k 回答"够不够用"（Agent 只看前几条），MRR 回答"排得
    准不准"。只报其中一个都会让改动的效果看不全——真机上"按论文轮转"这条改动几乎
    不动 hit@1，却把未命中从 6 条降到 2 条。
    """

    channel: str = Field(description="Retrieval channel name.")
    scored: int = Field(ge=0, description="Queries whose gold exists in the corpus.")
    hit_at_1: int = Field(ge=0)
    hit_at_3: int = Field(ge=0)
    hit_at_5: int = Field(ge=0)
    hit_at_10: int = Field(ge=0)
    missed: int = Field(ge=0, description="Queries returning no gold paper at all.")
    mrr: float = Field(ge=0.0, le=1.0, description="Mean reciprocal rank over `scored`.")
    outcomes: list[QueryOutcome] = Field(default_factory=list)


class RetrievalBenchReport(BaseModel):
    """一次 known-item 基准的完整结果。"""

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    query_set: str
    corpus_ref: str
    corpus_papers: int = Field(ge=0)
    corpus_chunks: int = Field(ge=0)
    embedding_model: str = Field(default="")
    unusable_queries: list[str] = Field(
        default_factory=list,
        description=(
            "Queries excluded because no gold paper is in this corpus. They are not "
            "retrieval failures and must never enter a channel's denominator."
        ),
    )
    channels: list[ChannelScore] = Field(default_factory=list)
    top_k: int = Field(default=10, ge=1)


class DeliveryOverlapReport(BaseModel):
    """同一查询若干次调研之间，交付论文集合的重合度。"""

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    query: str = Field(default="", description="Survey topic these runs shared.")
    label: str = Field(default="", description="What is being compared, e.g. a commit.")
    runs: int = Field(ge=2)
    delivered: list[int] = Field(description="Papers delivered by each run, in order.")
    mean_jaccard: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Mean pairwise Jaccard. The headline number: below ~0.3 the corpus is "
            "effectively a fresh random draw each run and no A/B over it can mean much."
        ),
    )
    min_jaccard: float = Field(ge=0.0, le=1.0)
    stable_core: list[str] = Field(
        default_factory=list, description="Papers every run delivered."
    )
    union_size: int = Field(ge=0, description="Distinct papers any run delivered.")


class PaperHealth(BaseModel):
    """一篇论文在语料里的结构体检结果。"""

    paper_id: str
    title: str = Field(default="")
    chunks: int = Field(ge=0)
    sentences: int = Field(ge=0)
    has_abstract_chunk: bool = Field(
        description="A chunk of kind 'abstract' exists; the corpus front door."
    )
    anchor_kind: str = Field(
        default="",
        description=(
            "Kind of the chunk paper_corpus_overview and citation edges point at. "
            "Anything other than 'abstract' means both fell back."
        ),
    )
    anchor_prose_chars: int = Field(
        ge=0,
        description=(
            "Letters-bearing prose in the anchor's overview window. Low values mean "
            "the summary shown to the Ideator is title/author/LaTeX preamble."
        ),
    )
    sections: list[str] = Field(default_factory=list)
    outbound_citations: int = Field(ge=0, description="In-corpus papers this one cites.")
    visual_links: int = Field(ge=0)


class CorpusHealthReport(BaseModel):
    """语料的结构不变量体检。

    这份报告存在的理由：``final_status`` 只看"转换出东西没有、corpus_ref 拿到没有"，
    因此一份 10 篇论文、其中 5 篇没有摘要、彼此 0 条引用边、没有一篇讲主指标的语料会被
    判成 ``complete``。"跑得通"和"够用"是两件事，这里量的是后者。
    """

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    corpus_ref: str
    papers: int = Field(ge=0)
    chunks: int = Field(ge=0)
    sentences: int = Field(ge=0)
    semantic_search: bool = Field(description="Corpus carries sentence embeddings.")
    embedding_model: str = Field(default="")
    abstract_coverage: float = Field(
        ge=0.0, le=1.0, description="Fraction of papers with an abstract chunk."
    )
    thin_anchor_papers: int = Field(
        ge=0, description="Papers whose overview summary is nearly prose-free."
    )
    citation_edges: int = Field(ge=0, description="Distinct in-corpus citation edges.")
    citation_density: float = Field(
        ge=0.0, description="Citation edges per paper; near zero makes paper_cites inert."
    )
    visual_links: int = Field(ge=0)
    section_coverage: dict[str, int] = Field(
        default_factory=dict,
        description="Papers carrying each probed section name, after alias expansion.",
    )
    papers_detail: list[PaperHealth] = Field(default_factory=list)

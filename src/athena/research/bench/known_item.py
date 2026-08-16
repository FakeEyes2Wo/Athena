"""known-item 检索基准：给一句改写过的提问，看能不能找回那篇论文。

指标选择本身会决定结论，所以这里说清楚为什么是 known-item。早先用"返回片段里含不含
主题词"打分时，关键词与语义两个通道都是 P@5 = 1.00——那个指标对关键词检索近乎同义反复
（它本来就是按那些词匹配的），两个通道因此无从区分。换成"这句改写的提问能不能找回那
篇特定论文"才有区分度，实测两通道 MRR 差 2 倍。

计分只看**首个金标论文的名次**，不看它命中了哪个 chunk：Ideator 要的是"这篇论文进没进
我的视野"，一篇论文的哪一段先被返回不改变这件事。
"""

from collections.abc import Awaitable, Callable

from athena.research.bench.schemas import (
    ChannelScore,
    QueryOutcome,
    QuerySet,
    RetrievalBenchReport,
)
from athena.research.paper_rag.index import normalize
from athena.research.paper_rag.interfaces import TextEmbedder
from athena.research.paper_rag.schemas import SearchHit
from athena.research.paper_rag.search import (
    LoadedCorpus,
    corpus_paper_ids,
    keyword_search,
    semantic_search,
)

DEFAULT_TOP_K = 10
HIT_CUTOFFS = (1, 3, 5, 10)

KEYWORD_CHANNEL = "paper_keyword_search"
SEMANTIC_CHANNEL = "paper_semantic_search"

ChannelRunner = Callable[[str, list[str], int], Awaitable[list[SearchHit]]]


def distinct_papers(hits: list[SearchHit]) -> list[str]:
    """按返回顺序取出去重后的论文 id。

    名次按论文算而不是按 chunk 算：同一篇论文连出三个 chunk 不该让它的名次变成 1、2、3，
    否则"名额被一篇吃光"这种失败反而会让分数变好看。
    """
    seen: list[str] = []
    for hit in hits:
        if hit.paper_id and hit.paper_id not in seen:
            seen.append(hit.paper_id)
    return seen


def first_gold_rank(returned: list[str], gold: list[str]) -> int | None:
    """首个金标论文的 1-based 名次；一个都没返回时为 ``None``。"""
    wanted = set(gold)
    for position, paper_id in enumerate(returned, start=1):
        if paper_id in wanted:
            return position
    return None


def score_channel(channel: str, outcomes: list[QueryOutcome]) -> ChannelScore:
    """把逐条结果汇成一个通道的成绩。

    分母是 ``outcomes`` 的长度，而调用方只把"金标确实在语料里"的查询放进来——金标不在
    语料里是出题错误或语料构成变化，把它算进未命中会让检索背一个不属于它的锅。
    """
    ranks = [item.rank for item in outcomes]
    hits = {
        cutoff: sum(1 for rank in ranks if rank is not None and rank <= cutoff)
        for cutoff in HIT_CUTOFFS
    }
    reciprocal = sum(1.0 / rank for rank in ranks if rank is not None)
    scored = len(outcomes)
    return ChannelScore(
        channel=channel,
        scored=scored,
        hit_at_1=hits[1],
        hit_at_3=hits[3],
        hit_at_5=hits[5],
        hit_at_10=hits[10],
        missed=sum(1 for rank in ranks if rank is None),
        mrr=(reciprocal / scored) if scored else 0.0,
        outcomes=outcomes,
    )


async def run_channel(
    query_set: QuerySet,
    usable: list[str],
    channel: str,
    runner: ChannelRunner,
    top_k: int,
) -> ChannelScore:
    """在一个通道上跑完整组可用查询。"""
    wanted = set(usable)
    outcomes: list[QueryOutcome] = []
    for query in query_set.queries:
        if query.query_id not in wanted:
            continue
        try:
            hits = await runner(query.question, query.keywords, top_k)
        except Exception as error:  # noqa: BLE001 - 一条查询失败不该丢掉整个通道的成绩
            outcomes.append(
                QueryOutcome(
                    query_id=query.query_id,
                    error=f"{type(error).__name__}: {error}",
                )
            )
            continue
        returned = distinct_papers(hits)
        outcomes.append(
            QueryOutcome(
                query_id=query.query_id,
                rank=first_gold_rank(returned, query.gold),
                returned_papers=returned,
            )
        )
    return score_channel(channel, outcomes)


def keyword_runner(corpus: LoadedCorpus) -> ChannelRunner:
    """词面通道：喂 ``keywords``，退回到问题分词只是为了不让缺字段变成零分。"""

    async def run(question: str, keywords: list[str], top_k: int) -> list[SearchHit]:
        terms = keywords or question.split()
        return keyword_search(corpus, terms, top_k)

    return run


def semantic_runner(corpus: LoadedCorpus, embedder: TextEmbedder) -> ChannelRunner:
    """语义通道：把提问原样编码后做句级余弦检索。

    每条查询各编码一次，不做缓存——基准要量的是通道本身的检索质量，缓存只会让第二次
    重跑的耗时数字失真。生产路径该不该缓存是另一件事。
    """

    async def run(question: str, _keywords: list[str], top_k: int) -> list[SearchHit]:
        vectors = await embedder.embed([question])
        if not vectors:
            return []
        return semantic_search(corpus, normalize(vectors[0]), top_k)

    return run


def usable_queries(query_set: QuerySet, corpus: LoadedCorpus) -> tuple[list[str], list[str]]:
    """把查询分成"金标在语料里"与"无从回答"两组。

    这一步在真机上当场抓出过一个错标——《When AUC meets DRO》通篇用 KL 散度与 CVaR，
    从不出现 Wasserstein，它不该是那道题的答案。没有这一步，出题错误会伪装成检索失败。
    """
    present = corpus_paper_ids(corpus)
    usable: list[str] = []
    unusable: list[str] = []
    for query in query_set.queries:
        if any(paper_id in present for paper_id in query.gold):
            usable.append(query.query_id)
        else:
            unusable.append(query.query_id)
    return usable, unusable


async def run_known_item(
    corpus: LoadedCorpus,
    query_set: QuerySet,
    *,
    corpus_ref: str = "",
    embedder: TextEmbedder | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> RetrievalBenchReport:
    """跑完一组 known-item 查询，返回逐通道成绩。

    没有 ``embedder`` 时只跑词面通道而不是报错：关键词检索的回归照样值得盯，而语义通道
    需要与建索引时同一个编码器，那是运行环境的事，不该让整个基准跑不起来。
    """
    usable, unusable = usable_queries(query_set, corpus)
    channels = [
        await run_channel(
            query_set, usable, KEYWORD_CHANNEL, keyword_runner(corpus), top_k
        )
    ]
    if embedder is not None and corpus.has_vectors():
        channels.append(
            await run_channel(
                query_set,
                usable,
                SEMANTIC_CHANNEL,
                semantic_runner(corpus, embedder),
                top_k,
            )
        )
    return RetrievalBenchReport(
        query_set=query_set.name,
        corpus_ref=corpus_ref,
        corpus_papers=len(corpus_paper_ids(corpus)),
        corpus_chunks=len(corpus.index.entries),
        embedding_model=corpus.index.embedding_model,
        unusable_queries=unusable,
        channels=channels,
        top_k=top_k,
    )


def compare(before: RetrievalBenchReport, after: RetrievalBenchReport) -> list[str]:
    """逐条比对两次基准，返回人可读的变化行。

    汇总指标会把互相抵消的变化藏起来：真机上引入 IDF 让 MRR 从 0.468 升到 0.530，同时
    把一道题从第 9 名打成未命中——只看 MRR 就会把这次交换当成纯改进。
    """
    lines: list[str] = []
    after_by_channel = {item.channel: item for item in after.channels}
    for channel in before.channels:
        later = after_by_channel.get(channel.channel)
        if later is None:
            lines.append(f"{channel.channel}: 通道消失")
            continue
        if abs(later.mrr - channel.mrr) >= 1e-9:
            lines.append(
                f"{channel.channel}: MRR {channel.mrr:.3f} -> {later.mrr:.3f}"
                f"  未命中 {channel.missed} -> {later.missed}"
            )
        ranks = {item.query_id: item.rank for item in channel.outcomes}
        for outcome in later.outcomes:
            was = ranks.get(outcome.query_id, "n/a")
            if was != outcome.rank:
                lines.append(
                    f"  {channel.channel} {outcome.query_id}: "
                    f"{was if was is not None else '未命中'} -> "
                    f"{outcome.rank if outcome.rank is not None else '未命中'}"
                )
    return lines


__all__ = [
    "DEFAULT_TOP_K",
    "KEYWORD_CHANNEL",
    "SEMANTIC_CHANNEL",
    "compare",
    "distinct_papers",
    "first_gold_rank",
    "run_known_item",
    "score_channel",
    "usable_queries",
]

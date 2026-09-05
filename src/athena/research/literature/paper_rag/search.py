"""A-RAG 检索与遍历算子的实现，与工具边界分离以便直接单测。

两类算子：**检索**（关键词、语义）按内容找入口，**遍历**（图文互链、引用、章节）沿已
有的类型化边走一步。

装载状态分两层，因为它们的共享范围本来就不同：``CorpusCache`` 持有解码后的只读语料，
按 ``corpus_ref`` 在进程内共享；``RetrievalSession`` 持有"本会话已整篇读过哪些 chunk"，
按 Agent 各自独立。合成一个对象时，几条并行的 Ideator lane 要么各解一份 1 GB 级的
语料，要么互相吃掉对方的正文。
"""

import asyncio
from dataclasses import dataclass

import numpy

from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.research.literature.paper_rag.index import (
    LETTER_RUN,
    anchor_index,
    decode_json_vectors,
    normalize,
    unpack_vectors,
)
from athena.research.literature.paper_rag.models import (
    ChunkRead,
    CorpusOverview,
    PaperCorpusIndex,
    PaperSummary,
    SearchHit,
)

SENTENCE_POOL_FACTOR = 8
SELF_CONTAINED_TOKENS = 5
ABSTRACT_KIND = "abstract"
# 一次总览要同时装下几十篇论文，预算按"够判断值不值得读"给，不是按"读懂"给：44 篇真实
# 语料下整份结果约 25 KB，摘要放到 400 字符则要 37 KB，多出来的部分不改变任何取舍。
OVERVIEW_ABSTRACT_CHARS = 280
OVERVIEW_SECTIONS = 10
MAX_SNIPPET_CHARS = 600
TRAVERSAL_SNIPPET_SENTENCES = 3
ALREADY_READ_NOTICE = "This chunk has been read before."

# 章节别名：论文之间对"同一个部分"的叫法不统一，而按字面子串匹配会让跨论文对比这个
# 算子存在的理由落空。实测 44 篇真实语料里 ``Limitations`` 只命中 3 篇、``Ablation``
# 7 篇——不是这些论文不讨论局限与消融，是它们把内容放在别的标题下面。
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "limitation": ("limitation", "threats to validity", "shortcoming", "failure case"),
    "ablation": (
        "ablation",
        "component analysis",
        "sensitivity analysis",
        "factor analysis",
    ),
    "related work": ("related work", "background", "prior work", "literature"),
    "experiment": ("experiment", "empirical", "evaluation", "results"),
    "conclusion": ("conclusion", "concluding", "summary and outlook"),
    "discussion": ("discussion", "analysis and discussion"),
    "method": ("method", "methodology", "approach", "our model", "proposed"),
    "dataset": ("dataset", "data set", "data collection", "corpus", "benchmark"),
}


def heading_variants(heading: str) -> tuple[str, ...]:
    """把一个章节名展开成实际去匹配的若干写法。

    按"组内任一写法出现在查询里"来选组，而不是要求查询与某个写法字面相等：``Limitations``
    要能选中以 ``limitation`` 为词根的那一组，``Ablation Studies`` 同理。查询本身始终留
    在返回值里，因此未登记的章节名原样匹配——这层别名只放宽命中范围，从不改变语义。

    一个查询命中多组时取先声明的那组（如 ``dataset limitations``）；这种混合标题本来
    就没有唯一正确答案，取第一组至少是确定的。
    """
    wanted = heading.lower().strip()
    for aliases in SECTION_ALIASES.values():
        if any(alias in wanted for alias in aliases):
            return (wanted, *aliases)
    return (wanted,)


@dataclass(slots=True)
class LoadedCorpus:
    """解码后的语料：索引本体、chunk_id 映射、反向引用索引、以及按需装载的句向量。

    ``cited_by`` 是引用边的反向索引，装载时构建一次：正向"这个 chunk 引了谁"直接读
    ``entry.cited_ids`` 即可，反向"谁引了这篇"要遍历全部条目，每次查询重算不合算。
    ``lowered`` 是正文的小写副本，关键词检索每次查询都要用；不预算的话每次查询都要
    重新小写一遍全语料（实测 4.5 MB 正文 25 ms/次）。

    ``vectors``/``weights`` 只有语义检索需要，因此默认为空——关键词检索、整篇读取与
    三个遍历算子都不该为一份用不到的向量矩阵付装载代价。
    """

    index: PaperCorpusIndex
    positions: dict[str, int]
    cited_by: dict[str, list[str]]
    lowered: list[str]
    vectors: numpy.ndarray | None = None
    weights: numpy.ndarray | None = None

    def has_vectors(self) -> bool:
        """本语料是否已装载句向量。"""
        return self.vectors is not None


def self_contained_weight(text: str) -> float:
    """按实词数给句子一个 0..1 的自足度权重；``"$t = $ chunk_read"`` 返回 0.4。

    结构感知切句去掉的是纯结构片段，剩下的仍有一类噪声：实词寥寥的正文片段——公式行、
    数字表格行、参考文献条目的姓名部分。它们在余弦检索里占便宜，因为向量集中在少数维
    度上，命中查询里任意一个词就能逼近相似度上界，却不必覆盖查询的其余部分。按实词数
    线性降权，等于要求一个检索单元先足够自足，再谈相似度。

    只作用于语义检索：这类片段恰恰是关键词检索的正当目标（按公式名或作者名定位），
    因此 ``keyword_search`` 与索引本身都不受影响。
    """
    return min(1.0, len(LETTER_RUN.findall(text)) / SELF_CONTAINED_TOKENS)


class CorpusCache:
    """进程级语料缓存：同一 ``corpus_ref`` 只解码一次，多个 Agent 会话共用一份。

    解码结果是纯只读数据，因此可以共享；共享是必需的而不是优化——一份 44 篇论文的
    语料，句向量解码后常驻百 MB 级，三条并行的 Ideator lane 各解一份没有余量。

    每个 ``corpus_ref`` 一把锁：两条 lane 同时首次访问同一语料时，第二条等第一条解
    完直接命中缓存，而不是各解一遍。

    ``query_vectors`` 是查询向量缓存，也挂在这一层。语义检索 96% 的时间是一次编码 API
    往返（实测 p50 136.8 ms，本地打分只要 6.66 ms），而同一句查询会被跨 lane、跨轮次
    反复问到——三条 Ideator lane 拿到的是同一个任务、同一份语料，问出来的话高度重合。
    缓存按编码器分桶：不同模型的向量不在同一个空间里。
    """

    def __init__(self) -> None:
        self._corpora: dict[str, LoadedCorpus] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._query_vectors: dict[tuple[str, str], list[float]] = {}

    async def embed_query(self, embedder, query: str) -> list[float]:
        """编码一句查询，同一 ``(模型, 查询)`` 只付一次 API 往返。

        缓存归一化后的向量：``semantic_search`` 本来就要归一化，存归一化后的省一次计算，
        也让缓存值可以直接喂给矩阵乘。
        """
        key = (getattr(embedder, "model", ""), query)
        found = self._query_vectors.get(key)
        if found is not None:
            return found
        vectors = await embedder.embed([query])
        vector = normalize(vectors[0]) if vectors else []
        self._query_vectors[key] = vector
        return vector

    async def load(
        self, store: ArtifactStore, corpus_ref: ArtifactRef, *, vectors: bool = False
    ) -> LoadedCorpus:
        """载入并缓存语料；``vectors=True`` 时额外装载句向量与自足度权重。"""
        lock = self._locks.setdefault(corpus_ref, asyncio.Lock())
        async with lock:
            corpus = self._corpora.get(corpus_ref)
            if corpus is None:
                corpus = await _decode_corpus(store, corpus_ref)
                self._corpora[corpus_ref] = corpus
            if vectors and not corpus.has_vectors():
                await _attach_vectors(store, corpus)
            return corpus


async def _decode_corpus(store: ArtifactStore, corpus_ref: ArtifactRef) -> LoadedCorpus:
    """解析索引本体并预算好每次查询都要用的派生结构（不含句向量）。"""
    index = PaperCorpusIndex.model_validate_json(await store.get_text(corpus_ref))
    cited_by: dict[str, list[str]] = {}
    for entry in index.entries:
        for target in entry.cited_ids:
            cited_by.setdefault(target, []).append(entry.chunk_id)
    return LoadedCorpus(
        index=index,
        positions={
            entry.chunk_id: position for position, entry in enumerate(index.entries)
        },
        cited_by=cited_by,
        lowered=[entry.text.lower() for entry in index.entries],
    )


def _mapped_vectors(store: ArtifactStore, ref: ArtifactRef) -> numpy.ndarray | None:
    """尽量以 mmap 打开句向量文件；做不到时返回 ``None`` 让调用方走读字节的路径。

    读字节再解码要同时持有两份：151 MB 的字节缓冲加 151 MB 的数组，工作集 333 MB。
    ``mmap_mode="r"`` 实测 0.8 ms 打开、常驻 29.8 MB，首次查询触页后稳定在 186 MB——
    每个语料省约 147 MB，而且在内存压力下这部分可以被换出去。

    只有本地内容寻址存储能给出路径，因此按能力探测而不是按类型判断：``ArtifactStore``
    协议里没有 ``path_for``，将来换成远端实现时这里自动退回读字节。
    """
    path_for = getattr(store, "path_for", None)
    if path_for is None:
        return None
    try:
        path = path_for(ref)
        if not path.is_file():
            return None
        return numpy.load(path, mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError):
        # 路径拿不到、文件被清理、或不是 npy 格式 → 退回读字节，不让检索因此失败
        return None


async def _attach_vectors(store: ArtifactStore, corpus: LoadedCorpus) -> None:
    """装载句向量与自足度权重；未建向量的语料留空，由语义检索明确报错。"""
    index = corpus.index
    if index.embedding_ref is None:
        return
    if index.embedding_format == "float32":
        mapped = await asyncio.to_thread(_mapped_vectors, store, index.embedding_ref)
        corpus.vectors = (
            mapped
            if mapped is not None
            else unpack_vectors(await store.get_bytes(index.embedding_ref))
        )
    else:
        # 1.0 语料：磁盘上仍是 JSON 文本，解析一次后与新格式共用同一种内存表示
        corpus.vectors = await asyncio.to_thread(
            decode_json_vectors, await store.get_text(index.embedding_ref)
        )
    corpus.weights = numpy.fromiter(
        (
            self_contained_weight(index.sentence_text(position))
            for position in range(len(index.sentences))
        ),
        dtype=numpy.float32,
        count=len(index.sentences),
    )


class RetrievalSession:
    """一个 Agent 的检索会话：共享的语料缓存 + 自己的已读集合。

    已读集合让重复 ``paper_chunk_read`` 退化为零成本回执，既省上下文也促使 Agent 去
    探索新的 chunk。它必须按 Agent 独立：共享会让第二条 lane 因为第一条读过而拿不到
    正文。缺省自带一个私有 ``CorpusCache``，供单测与单 Agent 场景直接使用。
    """

    def __init__(self, cache: CorpusCache | None = None) -> None:
        self._cache = cache or CorpusCache()
        self._read: set[str] = set()

    async def load(
        self, store: ArtifactStore, corpus_ref: ArtifactRef, *, vectors: bool = False
    ) -> LoadedCorpus:
        """经共享缓存载入语料；``vectors=True`` 只由语义检索传。"""
        return await self._cache.load(store, corpus_ref, vectors=vectors)

    async def embed_query(self, embedder, query: str) -> list[float]:
        """经共享缓存编码一句查询；见 ``CorpusCache.embed_query``。"""
        return await self._cache.embed_query(embedder, query)

    def was_read(self, chunk_id: str) -> bool:
        """本会话内是否已整篇读过该 chunk。"""
        return chunk_id in self._read

    def mark_read(self, chunk_id: str) -> None:
        """登记一次整篇读取。"""
        self._read.add(chunk_id)

    def read_chunk_ids(self) -> set[str]:
        """本会话整篇读过的 chunk id；引用支持性判定要拿它们取正文。"""
        return set(self._read)

    def read_papers(self) -> set[str]:
        """本会话真正打开过正文的论文集合，用于核验假设的 ``sources``。

        判据刻意取"整篇读过"而不是"检索命中过"。真机（2026-08-16 第 12 次）里 Ideator
        把一篇《数据增强综述》引来支持"两两交互特征"、把一篇《信用卡欺诈检测综述》同时
        引来支持 target encoding 和 SMOTE——这些论文都在检索结果里出现过，只是从没被
        打开。按命中算就拦不住这种贴标签式引用；按读过算才能。
        """
        return {paper_namespace(chunk_id) for chunk_id in self._read}


def corpus_overview(
    corpus: LoadedCorpus, paper_ids: list[str], limit: int
) -> CorpusOverview:
    """列出语料里有哪些论文，每篇给标题、摘要开头、可用章节名与 chunk 数。

    这是 Agent 拿到 ``corpus_ref`` 之后唯一不需要先知道点什么的入口。其余七个算子都
    要求先有关键词、查询、chunk id 或章节名——没有本算子，第一步只能瞎猜。它同时是
    ``sources`` 的合法取值来源：这里列出的 ``paper_id`` 就是可以引用的键。

    纯索引读取：不调模型、不访网络、不碰句向量。
    """
    index = corpus.index
    allowed = {item for item in paper_ids if item}
    grouped: dict[str, list[int]] = {}
    for position, entry in enumerate(index.entries):
        if allowed and entry.paper_id not in allowed:
            continue
        grouped.setdefault(entry.paper_id, []).append(position)
    return CorpusOverview(
        papers=len({entry.paper_id for entry in index.entries}),
        chunks=len(index.entries),
        semantic_search=index.embedding_ref is not None,
        embedding_model=index.embedding_model,
        summaries=[
            _paper_summary(corpus, paper_id, positions)
            for paper_id, positions in list(grouped.items())[:limit]
        ],
    )


def corpus_paper_ids(corpus: LoadedCorpus) -> set[str]:
    """语料里真实存在的 paper id；是"这条引用是不是编的"唯一的判据。"""
    return {entry.paper_id for entry in corpus.index.entries if entry.paper_id}


def _paper_summary(
    corpus: LoadedCorpus, paper_id: str, positions: list[int]
) -> PaperSummary:
    """按一篇论文的 chunk 位置组装门面；锚点优先摘要，与索引里的引用落点同一规则。"""
    entries = corpus.index.entries
    # 与建索引时的引用落点同一条规则（``index.anchor_index``）：总览摘要与引用边指向
    # 的必须是同一个 chunk，否则"去读引用指向的那篇"读到的和目录里写的不是一段。
    anchor = positions[
        anchor_index(
            [entries[position].kind for position in positions],
            [entries[position].text for position in positions],
            [entries[position].title for position in positions],
        )
    ]
    sections: list[str] = []
    for position in positions:
        path = entries[position].heading_path
        if path and path[0] not in sections:
            sections.append(path[0])
    text = entries[anchor].text.strip()
    return PaperSummary(
        paper_id=paper_id,
        title=entries[anchor].title,
        anchor_chunk_id=entries[anchor].chunk_id,
        abstract=(
            text
            if len(text) <= OVERVIEW_ABSTRACT_CHARS
            else text[:OVERVIEW_ABSTRACT_CHARS].rstrip() + " …"
        ),
        chunks=len(positions),
        sections=sections[:OVERVIEW_SECTIONS],
    )


def join_snippet(sentences: list[str]) -> str:
    """把命中的句子拼成 snippet，超出预算处截断。

    上游 chunk 可达数千字符，不设上限会让"只返回片段"的设计失效。
    """
    text = " ".join(part.strip() for part in sentences if part.strip())
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    return text[:MAX_SNIPPET_CHARS].rstrip() + " …"


def make_hit(
    corpus: LoadedCorpus, position: int, score: float, snippet: str
) -> SearchHit:
    """按 chunk 位置组装检索命中。"""
    entry = corpus.index.entries[position]
    return SearchHit(
        chunk_id=entry.chunk_id,
        paper_id=entry.paper_id,
        title=entry.title,
        kind=entry.kind,
        heading_path=entry.heading_path,
        score=score,
        snippet=snippet,
        visual_ids=entry.visual_ids,
        cited_ids=entry.cited_ids,
    )


def _head_snippet(corpus: LoadedCorpus, position: int) -> str:
    entry = corpus.index.entries[position]
    return join_snippet(
        [
            corpus.index.sentence_text(index)
            for index in range(entry.sentence_start, entry.sentence_end)
        ][:TRAVERSAL_SNIPPET_SENTENCES]
    )


def traverse(corpus: LoadedCorpus, targets: list[str]) -> list[SearchHit]:
    """Return linked chunks in first-seen order, with orientation snippets."""
    results: list[SearchHit] = []
    seen: set[str] = set()
    for chunk_id in targets:
        position = corpus.positions.get(chunk_id)
        if position is None or chunk_id in seen:
            continue
        seen.add(chunk_id)
        results.append(make_hit(corpus, position, 1.0, _head_snippet(corpus, position)))
    return results


def edge_targets(corpus: LoadedCorpus, chunk_ids: list[str], edge: str) -> list[str]:
    """Resolve an entry edge in input order."""
    targets: list[str] = []
    for chunk_id in chunk_ids:
        position = corpus.positions.get(chunk_id)
        if position is not None:
            targets.extend(getattr(corpus.index.entries[position], edge))
    return targets


def paper_namespace(chunk_id: str) -> str:
    """Extract the paper namespace from a namespaced chunk id."""
    return chunk_id.rsplit(":", 1)[0]


def citing_anchors(corpus: LoadedCorpus, chunk_ids: list[str]) -> list[str]:
    """Return cited-paper anchors for the supplied chunks."""
    wanted = {paper_namespace(chunk_id) for chunk_id in chunk_ids}
    return [anchor for anchor in corpus.cited_by if paper_namespace(anchor) in wanted]


def visual_links(corpus: LoadedCorpus, chunk_ids: list[str]) -> list[SearchHit]:
    """Follow visual links from the supplied chunks."""
    return traverse(corpus, edge_targets(corpus, chunk_ids, "visual_ids"))


def citation_links(
    corpus: LoadedCorpus, chunk_ids: list[str], direction: str
) -> list[SearchHit]:
    """Follow outgoing citations or incoming citing-paper links."""
    if direction != "cited_by":
        return traverse(corpus, edge_targets(corpus, chunk_ids, "cited_ids"))
    anchors = sorted(citing_anchors(corpus, chunk_ids))
    return traverse(
        corpus, [target for anchor in anchors for target in corpus.cited_by[anchor]]
    )


def section_search(
    corpus: LoadedCorpus, heading: str, paper_ids: list[str], limit: int
) -> list[SearchHit]:
    """Find matching sections and interleave results by paper."""
    variants = heading_variants(heading)
    allowed = {item for item in paper_ids if item}
    by_paper: dict[str, list[tuple[float, int]]] = {}
    for position, entry in enumerate(corpus.index.entries):
        if allowed and entry.paper_id not in allowed:
            continue
        depth = next(
            (
                level
                for level, name in enumerate(entry.heading_path)
                if any(variant in name.lower() for variant in variants)
            ),
            None,
        )
        if depth is not None:
            by_paper.setdefault(entry.paper_id, []).append(
                (1.0 / (1 + depth), position)
            )
    for group in by_paper.values():
        group.sort(key=lambda item: (-item[0], item[1]))
    order = sorted(by_paper, key=lambda paper: (-by_paper[paper][0][0], paper))
    rounds = max((len(group) for group in by_paper.values()), default=0)
    picked = [
        by_paper[paper][index]
        for index in range(rounds)
        for paper in order
        if index < len(by_paper[paper])
    ]
    return [
        make_hit(corpus, position, score, _head_snippet(corpus, position))
        for score, position in picked[:limit]
    ]


def read_one(
    corpus: LoadedCorpus, session: RetrievalSession, position: int
) -> ChunkRead:
    """Read one chunk, suppressing text already read in this session."""
    entry = corpus.index.entries[position]
    if session.was_read(entry.chunk_id):
        return ChunkRead(
            chunk_id=entry.chunk_id,
            status="already_read",
            title=entry.title,
            heading_path=entry.heading_path,
            text=ALREADY_READ_NOTICE,
        )
    session.mark_read(entry.chunk_id)
    return ChunkRead(
        chunk_id=entry.chunk_id,
        status="read",
        title=entry.title,
        heading_path=entry.heading_path,
        text=entry.text,
        visual_ids=entry.visual_ids,
        cited_ids=entry.cited_ids,
    )


def read_chunks(
    corpus: LoadedCorpus,
    session: RetrievalSession,
    chunk_ids: list[str],
    include_adjacent: bool,
) -> list[ChunkRead]:
    """Read requested chunks and optional same-paper neighbours in input order."""
    results: list[ChunkRead] = []
    seen: set[int] = set()
    for chunk_id in chunk_ids:
        position = corpus.positions.get(chunk_id)
        if position is None:
            results.append(ChunkRead(chunk_id=chunk_id, status="not_found"))
            continue
        targets = [position]
        if include_adjacent:
            paper_id = corpus.index.entries[position].paper_id
            targets = [
                other
                for other in (position - 1, position, position + 1)
                if 0 <= other < len(corpus.index.entries)
                and corpus.index.entries[other].paper_id == paper_id
            ]
        for target in targets:
            if target not in seen:
                seen.add(target)
                results.append(read_one(corpus, session, target))
    return results


def score_by_keywords(
    corpus: LoadedCorpus, terms: list[str]
) -> list[tuple[float, int]]:
    """按 ``Σ 词频 × 关键词长度`` 给每个命中 chunk 打分，得分降序、同分按 chunk 序。"""
    scored = [
        (float(sum(lowered.count(term) * len(term) for term in terms)), position)
        for position, lowered in enumerate(corpus.lowered)
    ]
    hits = [item for item in scored if item[0] > 0.0]
    hits.sort(key=lambda item: (-item[0], item[1]))
    return hits


def rotate_by_paper(
    corpus: LoadedCorpus, scored: list[tuple[float, int]], limit: int
) -> list[tuple[float, int]]:
    """按论文轮转分配名额：每篇先出最高分的一条，再出第二条。

    与 ``section_search`` 同一条纪律，理由也一样。实测（44 篇语料，query
    "false positive rate range / specificity / restricted"）：67 个 chunk 命中、
    来自 17 篇论文，而按全局得分直排时 **10 个名额有 9 个被同一篇吃掉**——一篇临床
    论文反复把 specificity 当指标名用。真正该出的那篇，最高分 chunk 排在全局第 17
    位，正好落在 k=10 之外；轮转后它排第 5。
    """
    by_paper: dict[str, list[tuple[float, int]]] = {}
    for item in scored:
        by_paper.setdefault(corpus.index.entries[item[1]].paper_id, []).append(item)
    order = sorted(by_paper, key=lambda paper: (-by_paper[paper][0][0], paper))
    rounds = max((len(group) for group in by_paper.values()), default=0)
    return [
        by_paper[paper][index]
        for index in range(rounds)
        for paper in order
        if index < len(by_paper[paper])
    ][:limit]


def keyword_search(
    corpus: LoadedCorpus, keywords: list[str], limit: int
) -> list[SearchHit]:
    """按 ``Σ 词频 × 关键词长度`` 给 chunk 打分，长关键词更具体因而权重更高。

    匹配不区分大小写，以便实体名在正文与标题的不同写法下都能命中。

    多词关键词按**字面子串**匹配，因此没有逐字出现过的短语一条都命中不了；实测 27 条
    自然多词关键词里 7 条（26%）返回空，而这 7 条拆成单词后全部有结果。``Wasserstein
    ball`` 命中 0、``wasserstein`` 命中 5，且全在那篇讲它的论文里。整组关键词颗粒无收
    时因此退到词级重试一次——对 Agent 而言"语料里没有"与"你的措辞没逐字出现"是两回事，
    而当前接口把后者伪装成前者。

    名额按论文轮转，见 ``rotate_by_paper``。
    """
    terms = [keyword.lower() for keyword in keywords if keyword.strip()]
    scored = score_by_keywords(corpus, terms)
    if not scored:
        tokens = [token for term in terms for token in term.split() if token]
        if len(tokens) > len(terms):
            terms = tokens
            scored = score_by_keywords(corpus, terms)

    hits: list[SearchHit] = []
    for score, position in rotate_by_paper(corpus, scored, limit):
        entry = corpus.index.entries[position]
        matched = [
            corpus.index.sentence_text(sentence)
            for sentence in range(entry.sentence_start, entry.sentence_end)
        ]
        snippet = join_snippet(
            [text for text in matched if any(term in text.lower() for term in terms)]
        )
        hits.append(make_hit(corpus, position, score, snippet))
    return hits


def semantic_search(
    corpus: LoadedCorpus, query_vector: list[float], limit: int
) -> list[SearchHit]:
    """句级余弦检索后按父 chunk 聚合，chunk 得分取其最高句得分。

    先取全局最高分的一批句子再聚合，因此一个 chunk 只有真正命中的句子会进入 snippet，
    而不是整段正文。相似度非正的句子一律排除，避免小语料下无关句被凑进 snippet。

    余弦相似度按 ``self_contained_weight`` 折算后再排序，实词寥寥的片段不会仅凭向量方向
    集中就压过真正回答查询的正文句。

    打分走 numpy 矩阵乘：逐句纯 Python 点积在 36920 句 × 1024 维的真实语料上要 1.9 秒，
    同一次计算 numpy 用 2.8 毫秒。排序仍按"得分降序、同分按句序"，与逐句实现一致。
    """
    if corpus.vectors is None or corpus.weights is None:
        return []
    query = numpy.asarray(normalize(query_vector), dtype=numpy.float32)
    scores = corpus.weights * (corpus.vectors @ query)
    positive = numpy.flatnonzero(scores > 0.0)
    # positive 本身升序，稳定排序因此让同分句按句序在前，与逐句实现的结果一致
    order = numpy.argsort(-scores[positive], kind="stable")
    pool = positive[order][: max(limit, 1) * SENTENCE_POOL_FACTOR]

    # pool 已按得分降序，因此每个 chunk 的首个句子就是它的最高分句
    grouped: dict[int, list[int]] = {}
    for sentence in pool.tolist():
        grouped.setdefault(corpus.index.sentences[sentence].entry_index, []).append(
            sentence
        )
    ranked = sorted(grouped.items(), key=lambda item: -scores[item[1][0]])[:limit]

    return [
        make_hit(
            corpus,
            position,
            float(scores[sentences[0]]),
            join_snippet([corpus.index.sentence_text(s) for s in sorted(sentences)]),
        )
        for position, sentences in ranked
    ]


RRF_K = 60
"""RRF 的平滑常数，取自 Cormack 等人的原始设定。

它决定"排在第 1 相对排在第 5 值多少"。60 是个刻意保守的取值：名次靠前的优势被压得比较
平，因此单个通道的一次误判不会主导融合结果——而这正是融合存在的理由。
"""


def reciprocal_rank_fusion(
    channels: list[list[SearchHit]], limit: int
) -> list[SearchHit]:
    """按 ``Σ 1/(k + 名次)`` 融合多个通道的结果。

    用名次而不是分数：两个通道的分数根本不可比——词面分是 ``Σ 词频 × 关键词长度``（无
    上界），语义分是加权余弦（0..1）。任何把它们线性相加的做法都要先猜一个归一化，而那
    个猜测会随语料大小漂移。名次不需要归一化。

    同一个 chunk 在多个通道里都出现时得分累加，这正是融合想要的：两条独立证据都指向它。
    命中体本身取首次出现的那个，因为不同通道给的 snippet 不同，而先出现的那个来自排名
    更靠前的通道。
    """
    scores: dict[str, float] = {}
    first: dict[str, SearchHit] = {}
    for hits in channels:
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            first.setdefault(hit.chunk_id, hit)
    order = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [
        first[chunk_id].model_copy(update={"score": scores[chunk_id]})
        for chunk_id in order[:limit]
    ]


def hybrid_search(
    corpus: LoadedCorpus,
    query_vector: list[float],
    keywords: list[str],
    limit: int,
) -> list[SearchHit]:
    """词面与语义两个通道各取一批，再按 RRF 融合。

    两个通道的失效模式不同，而且不相关：词面检索跨不过措辞差异（"用一个从 Beta 分布抽
    出的系数混合两个训练样本"连不到 mixup 那篇论文上，因为论文根本不用这些词），语义
    检索则会在查询里出现精确术语或数字时被泛化的近义句挤掉。融合的价值全在这个互补性
    上——两个通道同时错的情况远少于任一通道单独错。

    各通道取 ``limit`` 的两倍再融合：只取 ``limit`` 会让"在 A 里排第 12、在 B 里排第 3"
    的 chunk 拿不到 A 的那一票，而那正是融合该救回来的那种命中。
    """
    pool = max(limit, 1) * 2
    channels = [
        keyword_search(corpus, keywords, pool) if keywords else [],
        semantic_search(corpus, query_vector, pool) if query_vector else [],
    ]
    return reciprocal_rank_fusion([item for item in channels if item], limit)

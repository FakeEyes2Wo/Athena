"""A-RAG 检索与遍历算子的实现，与工具边界分离以便直接单测。

两类算子：**检索**（关键词、语义）按内容找入口，**遍历**（图文互链、引用、章节）沿已
有的类型化边走一步。全部算子共享一个 ``RetrievalSession``：它缓存已解码的语料，避
免每次工具调用重复解析大 artifact，同时记录本会话已整篇读过的 chunk。
"""

import json
from dataclasses import dataclass

from athena.core.schemas import ArtifactRef
from athena.research.paper_rag.index import LETTER_RUN, normalize
from athena.research.paper_rag.schemas import ChunkRead, PaperCorpusIndex, SearchHit
from athena.storage.artifact_store import ArtifactStore

MAX_SNIPPET_CHARS = 600
SENTENCE_POOL_FACTOR = 8
SELF_CONTAINED_TOKENS = 5
TRAVERSAL_SNIPPET_SENTENCES = 3
ALREADY_READ_NOTICE = "This chunk has been read before."


@dataclass(slots=True)
class LoadedCorpus:
    """解码后的语料：索引本体、句向量、自足度权重、以及 chunk_id 到位置的映射。

    ``cited_by`` 是引用边的反向索引，装载时构建一次：正向"这个 chunk 引了谁"直接读
    ``entry.cited_ids`` 即可，反向"谁引了这篇"要遍历全部条目，每次查询重算不合算。
    """

    index: PaperCorpusIndex
    vectors: list[list[float]]
    weights: list[float]
    positions: dict[str, int]
    cited_by: dict[str, list[str]]


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


class RetrievalSession:
    """三个检索工具共享的会话状态。

    语料索引按 ``corpus_ref`` 缓存，同一引用只解析一次；已读集合让重复
    ``paper_chunk_read`` 退化为零成本回执，既省上下文也促使 Agent 去探索新的 chunk。
    """

    def __init__(self) -> None:
        self._corpora: dict[str, LoadedCorpus] = {}
        self._read: set[str] = set()

    async def load(self, store: ArtifactStore, corpus_ref: ArtifactRef) -> LoadedCorpus:
        """载入并缓存语料；未建向量的语料不会去读句向量，也不会算自足度权重。"""
        cached = self._corpora.get(corpus_ref)
        if cached is not None:
            return cached
        index = PaperCorpusIndex.model_validate_json(await store.get_text(corpus_ref))
        vectors: list[list[float]] = []
        weights: list[float] = []
        if index.embedding_ref is not None:
            vectors = json.loads(await store.get_text(index.embedding_ref))
            weights = [
                self_contained_weight(index.sentence_text(position))
                for position in range(len(index.sentences))
            ]
        cited_by: dict[str, list[str]] = {}
        for entry in index.entries:
            for target in entry.cited_ids:
                cited_by.setdefault(target, []).append(entry.chunk_id)
        loaded = LoadedCorpus(
            index=index,
            vectors=vectors,
            weights=weights,
            positions={
                entry.chunk_id: position for position, entry in enumerate(index.entries)
            },
            cited_by=cited_by,
        )
        self._corpora[corpus_ref] = loaded
        return loaded

    def was_read(self, chunk_id: str) -> bool:
        """本会话内是否已整篇读过该 chunk。"""
        return chunk_id in self._read

    def mark_read(self, chunk_id: str) -> None:
        """登记一次整篇读取。"""
        self._read.add(chunk_id)


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


def keyword_search(
    corpus: LoadedCorpus, keywords: list[str], limit: int
) -> list[SearchHit]:
    """按 ``Σ 词频 × 关键词长度`` 给 chunk 打分，长关键词更具体因而权重更高。

    匹配不区分大小写，以便实体名在正文与标题的不同写法下都能命中。
    """
    terms = [keyword.lower() for keyword in keywords if keyword.strip()]
    scored: list[tuple[float, int]] = []
    for position, entry in enumerate(corpus.index.entries):
        lowered = entry.text.lower()
        score = float(sum(lowered.count(term) * len(term) for term in terms))
        if score > 0.0:
            scored.append((score, position))
    scored.sort(key=lambda item: (-item[0], item[1]))

    hits: list[SearchHit] = []
    for score, position in scored[:limit]:
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
    """
    query = normalize(query_vector)
    scores = [
        corpus.weights[position]
        * sum(left * right for left, right in zip(query, vector))
        for position, vector in enumerate(corpus.vectors)
    ]
    pool = sorted(
        (position for position, score in enumerate(scores) if score > 0.0),
        key=lambda position: -scores[position],
    )[: max(limit, 1) * SENTENCE_POOL_FACTOR]

    # pool 已按得分降序，因此每个 chunk 的首个句子就是它的最高分句
    grouped: dict[int, list[int]] = {}
    for sentence in pool:
        grouped.setdefault(corpus.index.sentences[sentence].entry_index, []).append(
            sentence
        )
    ranked = sorted(grouped.items(), key=lambda item: -scores[item[1][0]])[:limit]

    return [
        make_hit(
            corpus,
            position,
            scores[sentences[0]],
            join_snippet([corpus.index.sentence_text(s) for s in sorted(sentences)]),
        )
        for position, sentences in ranked
    ]


def head_snippet(corpus: LoadedCorpus, position: int) -> str:
    """取该 chunk 的开头几句作为定位用片段。

    遍历类算子没有查询向量可用来选句，因此固定取开头：正文 chunk 的开头通常就是它的
    主张句，图表单元的开头是图注。
    """
    entry = corpus.index.entries[position]
    return join_snippet(
        [
            corpus.index.sentence_text(sentence)
            for sentence in range(entry.sentence_start, entry.sentence_end)
        ][:TRAVERSAL_SNIPPET_SENTENCES]
    )


def traverse(corpus: LoadedCorpus, targets: list[str]) -> list[SearchHit]:
    """把一组 chunk id 组装成命中，保持给定顺序并去重、跳过不在语料内的 id。"""
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for chunk_id in targets:
        position = corpus.positions.get(chunk_id)
        if position is None or chunk_id in seen:
            continue
        seen.add(chunk_id)
        hits.append(make_hit(corpus, position, 1.0, head_snippet(corpus, position)))
    return hits


def edge_targets(corpus: LoadedCorpus, chunk_ids: list[str], edge: str) -> list[str]:
    """读取若干 chunk 的同一条类型化边，按给定顺序摊平；未知 chunk 直接跳过。"""
    targets: list[str] = []
    for chunk_id in chunk_ids:
        position = corpus.positions.get(chunk_id)
        if position is None:
            continue
        targets.extend(getattr(corpus.index.entries[position], edge))
    return targets


def paper_namespace(chunk_id: str) -> str:
    """取 chunk id 的论文命名空间；``"p1:chunk-a"`` 返回 ``"p1"``。"""
    return chunk_id.split(":", 1)[0]


def citing_anchors(corpus: LoadedCorpus, chunk_ids: list[str]) -> list[str]:
    """把任意 chunk id 归约到它所属论文"被引用时的落点"。

    引用边指向的是落点（摘要或首个单元），因此反向查询必须先归约，否则调用方传进来
    一个正文中段就会查不到任何引用者。
    """
    wanted = {paper_namespace(chunk_id) for chunk_id in chunk_ids}
    return [anchor for anchor in corpus.cited_by if paper_namespace(anchor) in wanted]


def visual_links(corpus: LoadedCorpus, chunk_ids: list[str]) -> list[SearchHit]:
    """沿图文互链走一步：正文 chunk 给出它讨论的图表，图表给出讨论它的正文。

    这条边此前和引用边一起塞在一个无类型列表里，Agent 只能盲跟；拆开之后"去看这段
    话讨论的那张图"是一次可命名的动作。
    """
    return traverse(corpus, edge_targets(corpus, chunk_ids, "visual_ids"))


def citation_links(
    corpus: LoadedCorpus, chunk_ids: list[str], direction: str
) -> list[SearchHit]:
    """沿引用边走一步。

    ``cites`` 是正向：给出这些 chunk 引用到的、语料内部论文的落点。``cited_by`` 是反
    向：给出语料里引用了这些论文的 chunk——对"谁在此基础上做了什么、谁反驳了它"这类
    问题，反向边比任何相似度都直接。MRAgent 把这两类分别称作 forward 与 reverse
    traversal，并指出正是反向遍历让 Agent 能根据已得证据改道。
    """
    if direction != "cited_by":
        return traverse(corpus, edge_targets(corpus, chunk_ids, "cited_ids"))
    anchors = sorted(citing_anchors(corpus, chunk_ids))
    return traverse(
        corpus,
        [target for anchor in anchors for target in corpus.cited_by[anchor]],
    )


def section_search(
    corpus: LoadedCorpus, heading: str, paper_ids: list[str], limit: int
) -> list[SearchHit]:
    """按章节名跨论文取 chunk，默认覆盖全语料。

    这是自顶向下的入口：对比性证据几乎总在另一篇论文的可比章节里（Limitations、
    Ablation、Related Work），而按假设措辞做语义检索只会优先返回同意它的段落。
    ``paper_ids`` 为空表示不限定论文——这正是该算子的默认用法。
    """
    wanted = heading.lower().strip()
    allowed = {item for item in paper_ids if item}
    scored: list[tuple[float, int]] = []
    for position, entry in enumerate(corpus.index.entries):
        if allowed and entry.paper_id not in allowed:
            continue
        depth = next(
            (
                level
                for level, name in enumerate(entry.heading_path)
                if wanted in name.lower()
            ),
            None,
        )
        if depth is not None:
            scored.append((1.0 / (1 + depth), position))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        make_hit(corpus, position, score, head_snippet(corpus, position))
        for score, position in scored[:limit]
    ]


def read_one(
    corpus: LoadedCorpus, session: RetrievalSession, position: int
) -> ChunkRead:
    """读取单个 chunk；本会话已读过的只回执提示，不再重复返回正文。"""
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
    """整篇读取指定 chunk，可选连同同一篇论文内的相邻 chunk 一并返回。"""
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
            if target in seen:
                continue
            seen.add(target)
            results.append(read_one(corpus, session, target))
    return results

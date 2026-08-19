"""paper_rag 的 Athena 工具边界：暴露给模型的分层检索与类型化遍历接口。

两类接口分工不同。**检索**（关键词、语义）按内容找入口；**遍历**（图文互链、引用、章
节）沿语料里已有的类型化边走一步。遍历算子分开命名而不是合并成一个通用
``related`` 是刻意的：MRAgent（ICML 2026）实测不同问题类型会激活完全不同的算子——时序
类问题 86% 的证据来自时间算子，多跳类主要来自标签与主题算子——一个通用相似度做不出这
种区分，而算子的选择本身就是 Agent 可观测、可评估的路由决策。

Agent loop 复用 Athena 已有的 ``core/agent``，这里只提供接口。检索与遍历都是纯读取因
而可并行；``paper_chunk_read`` 会更新会话已读集合，故按串行执行。
"""

import asyncio

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.paper_rag.interfaces import TextEmbedder
from athena.research.paper_rag.search import (
    RetrievalSession,
    citation_links,
    corpus_overview,
    hybrid_search,
    keyword_search,
    read_chunks,
    section_search,
    semantic_search,
    visual_links,
)
from athena.core.contracts import ArtifactStore

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
DEFAULT_OVERVIEW_PAPERS = 30
MAX_OVERVIEW_PAPERS = 100
CORPUS_REF_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Artifact reference of a paper corpus index built from paper_markdown output."
    ),
}
CHUNK_IDS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "description": "Chunk ids reported by a search or traversal tool.",
}
TOP_K_SCHEMA = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_TOP_K,
    "default": DEFAULT_TOP_K,
    "description": "Number of chunks to return.",
}


def require_chunk_ids(input: dict) -> list[str]:
    """校验并取出 ``chunk_ids``；``BaseTool`` 不校验 input schema，边界必须兜底。"""
    chunk_ids = input.get("chunk_ids")
    if not isinstance(chunk_ids, list) or not any(
        isinstance(item, str) and item.strip() for item in chunk_ids
    ):
        raise ValueError("chunk_ids must contain at least one non-empty string.")
    return [item for item in chunk_ids if isinstance(item, str) and item.strip()]


def require_corpus_ref(input: dict) -> str:
    """校验并取出 ``corpus_ref``，与其他 Athena 论文工具的入参约定一致。"""
    corpus_ref = input.get("corpus_ref")
    if not isinstance(corpus_ref, str) or not corpus_ref.strip():
        raise ValueError("corpus_ref must be a non-empty artifact reference.")
    return corpus_ref


def resolve_top_k(input: dict) -> int:
    """取出并夹紧 ``k``；``BaseTool`` 不校验 input schema，边界必须在代码里兜底。"""
    value = input.get("k", DEFAULT_TOP_K)
    if not isinstance(value, int) or value < 1:
        return DEFAULT_TOP_K
    return min(value, MAX_TOP_K)


def resolve_max_papers(input: dict) -> int:
    """取出并夹紧 ``max_papers``，与 ``resolve_top_k`` 同一条边界兜底约定。"""
    value = input.get("max_papers", DEFAULT_OVERVIEW_PAPERS)
    if not isinstance(value, int) or value < 1:
        return DEFAULT_OVERVIEW_PAPERS
    return min(value, MAX_OVERVIEW_PAPERS)


class PaperCorpusOverviewTool(BaseTool):
    """列出语料里有哪些论文，是拿到 ``corpus_ref`` 之后的第一步。

    其余算子都要求先知道点什么（关键词、查询、chunk id、章节名）；没有一个不需要前提
    的入口时，Agent 的第一次检索只能是猜。
    """

    spec = ToolSpec(
        name="paper_corpus_overview",
        description=(
            "List what is in a paper corpus: every paper's id, title, the opening of "
            "its abstract, how many chunks it has, and which section names it "
            "actually uses. Call this first when you are handed a corpus_ref — the "
            "other paper_* tools all need a keyword, query, chunk id or heading you "
            "do not have yet. The paper_id values it returns are the keys to record "
            "in a hypothesis's sources field."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "paper_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Restrict to these papers; omit to list them all.",
                },
                "max_papers": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_OVERVIEW_PAPERS,
                    "default": DEFAULT_OVERVIEW_PAPERS,
                    "description": "Number of papers to describe.",
                },
            },
            "required": ["corpus_ref"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self, artifacts: ArtifactStore, session: RetrievalSession | None = None
    ) -> None:
        self.artifacts = artifacts
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """列出语料内容。"""
        corpus_ref = require_corpus_ref(input)
        paper_ids = input.get("paper_ids") or []
        if not isinstance(paper_ids, list):
            raise ValueError("paper_ids must be an array of paper identifiers.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        corpus = await self.session.load(self.artifacts, corpus_ref)
        overview = corpus_overview(
            corpus,
            [item for item in paper_ids if isinstance(item, str)],
            resolve_max_papers(input),
        )
        return ToolResult(data=overview.model_dump())


class PaperKeywordSearchTool(BaseTool):
    """按精确关键词定位 chunk，返回 chunk id 与命中句片段。

    适合实体名、方法名、数据集名这类字面信号；不做同义扩展，因此没有语义漂移。
    """

    spec = ToolSpec(
        name="paper_keyword_search",
        description=(
            "Locate paper chunks containing exact keywords. Returns chunk ids with "
            "only the sentences that matched, not the full chunk. Best for entity, "
            "method, dataset, and metric names. Follow up with paper_chunk_read on "
            "the chunks worth reading in full, with paper_visual_of to reach the "
            "figures they discuss, or with paper_cites to reach the work they build "
            "on."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "keywords": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "description": "Literal terms to match; longer terms score higher.",
                },
                "k": TOP_K_SCHEMA,
            },
            "required": ["corpus_ref", "keywords"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self, artifacts: ArtifactStore, session: RetrievalSession | None = None
    ) -> None:
        self.artifacts = artifacts
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """执行一次关键词检索。"""
        corpus_ref = require_corpus_ref(input)
        keywords = input.get("keywords")
        if not isinstance(keywords, list) or not any(
            isinstance(item, str) and item.strip() for item in keywords
        ):
            raise ValueError("keywords must contain at least one non-empty string.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        corpus = await self.session.load(self.artifacts, corpus_ref)
        hits = keyword_search(
            corpus,
            [item for item in keywords if isinstance(item, str)],
            resolve_top_k(input),
        )
        return ToolResult(data={"hits": [hit.model_dump() for hit in hits]})


class PaperSemanticSearchTool(BaseTool):
    """按语义相似度定位 chunk，返回 chunk id 与命中句片段。

    句级编码后按父 chunk 聚合，取最高句得分，因此长 chunk 不会因为平均稀释而被埋没。
    """

    spec = ToolSpec(
        name="paper_semantic_search",
        description=(
            "Find paper chunks whose sentences are semantically closest to a natural "
            "language query. Returns chunk ids with only the matched sentences, not "
            "the full chunk. Best when the wording in the papers is unknown. Note "
            "that text agreeing with the query is what is closest to it, so this "
            "tool alone will under-report contrary evidence; use "
            "paper_section_search on Limitations or Ablation, or paper_cites with "
            "direction=cited_by, to reach work that disputes a claim."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Natural language description of what is sought.",
                },
                "k": TOP_K_SCHEMA,
            },
            "required": ["corpus_ref", "query"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        artifacts: ArtifactStore,
        embedder: TextEmbedder,
        session: RetrievalSession | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.embedder = embedder
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """执行一次语义检索；语料未建向量或模型不一致时明确报错。"""
        corpus_ref = require_corpus_ref(input)
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        # 唯一需要句向量的算子，因此也是唯一传 vectors=True 的调用点：其余五个算子
        # 不该为一份用不到的向量矩阵付装载代价。
        corpus = await self.session.load(self.artifacts, corpus_ref, vectors=True)
        if corpus.index.embedding_ref is None:
            return ToolResult(
                data={"hits": []},
                success=False,
                error="Corpus has no sentence embeddings; use paper_keyword_search.",
            )
        if corpus.index.embedding_model != self.embedder.model:
            return ToolResult(
                data={"hits": []},
                success=False,
                error=(
                    f"Corpus was embedded with '{corpus.index.embedding_model}' but "
                    f"the active embedder is '{self.embedder.model}'."
                ),
            )

        vector = await self.session.embed_query(self.embedder, query)
        hits = semantic_search(corpus, vector, resolve_top_k(input))
        return ToolResult(data={"hits": [hit.model_dump() for hit in hits]})


class PaperSearchTool(BaseTool):
    """默认入口：词面与语义两个通道各取一批，再按 RRF 融合。

    存在的理由是实测的互补性，不是设计上的对称。同一份 44 篇语料、12 条改写查询（金标
    是论文）上：

    ===================  ======  ======  ======  ======
    通道                 hit@1   hit@5   未命中  MRR
    ===================  ======  ======  ======  ======
    keyword              5/12    11/12   0       0.601
    semantic             9/12    11/12   1       0.833
    **hybrid (RRF)**     7/12    12/12   **0**   0.778
    ===================  ======  ======  ======  ======

    融合的 MRR 低于纯语义，**但它是唯一一条没有未命中的通道**，而且 hit@5 满分。这个
    取舍对本场景是对的：Ideator 的失败从来不是"该读的论文排在第 2 而不是第 1"，而是
    "它根本没进视野"。只看 MRR 会把这次交换读成退步。
    """

    spec = ToolSpec(
        name="paper_search",
        description=(
            "Search the corpus by meaning and by exact terms at once, fusing both "
            "rankings. This is the entry point to use first: it recovers papers that "
            "either channel alone misses. Returns chunk ids with matched snippets, "
            "not full chunks — read what looks promising with paper_chunk_read. "
            "Reach for paper_semantic_search or paper_keyword_search instead only "
            "when you deliberately want one channel: pure meaning, or a term that "
            "must appear verbatim."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Natural language description of what is sought.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact terms worth matching literally. Omit to derive them "
                        "from the query."
                    ),
                },
                "k": TOP_K_SCHEMA,
            },
            "required": ["corpus_ref", "query"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        artifacts: ArtifactStore,
        embedder: TextEmbedder,
        session: RetrievalSession | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.embedder = embedder
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """执行一次融合检索；没有向量时自动退化成纯词面而不是报错。

        退化而不报错是有理由的：融合的词面那一半在无向量语料上照常可用，把整个入口变成
        硬错误只会逼 Agent 去猜该换哪个工具。
        """
        corpus_ref = require_corpus_ref(input)
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        raw = input.get("keywords")
        keywords = [
            item.strip()
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, str) and item.strip()
        ] or query.split()
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        corpus = await self.session.load(self.artifacts, corpus_ref, vectors=True)
        vector: list[float] = []
        if (
            corpus.index.embedding_ref is not None
            and corpus.index.embedding_model == self.embedder.model
        ):
            vector = await self.session.embed_query(self.embedder, query)
        hits = hybrid_search(corpus, vector, keywords, resolve_top_k(input))
        return ToolResult(data={"hits": [hit.model_dump() for hit in hits]})


class PaperChunkReadTool(BaseTool):
    """整篇读取指定 chunk，本会话内重复读取只返回提示。

    检索工具只给片段，全文必须显式读取——这条渐进披露让上下文只装 Agent 判断过值得
    读的内容。
    """

    spec = ToolSpec(
        name="paper_chunk_read",
        description=(
            "Read the full text of chunks returned by the search or traversal tools. "
            "Chunks already read in this session return a short notice instead of "
            "their text, so re-reading costs nothing. Set include_adjacent to also "
            "read the neighbouring chunks of the same paper for surrounding context. "
            "Read only what a snippet or tag showed to be worth the context; use "
            "paper_visual_of and paper_cites to move between linked units instead of "
            "reading everything nearby."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "chunk_ids": CHUNK_IDS_SCHEMA,
                "include_adjacent": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Also read the previous and next chunk of the same paper."
                    ),
                },
            },
            "required": ["corpus_ref", "chunk_ids"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    def __init__(
        self, artifacts: ArtifactStore, session: RetrievalSession | None = None
    ) -> None:
        self.artifacts = artifacts
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """读取请求的 chunk 全文。"""
        corpus_ref = require_corpus_ref(input)
        chunk_ids = input.get("chunk_ids")
        if not isinstance(chunk_ids, list) or not any(
            isinstance(item, str) and item.strip() for item in chunk_ids
        ):
            raise ValueError("chunk_ids must contain at least one non-empty string.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        corpus = await self.session.load(self.artifacts, corpus_ref)
        chunks = read_chunks(
            corpus,
            self.session,
            [item for item in chunk_ids if isinstance(item, str)],
            bool(input.get("include_adjacent", False)),
        )
        return ToolResult(data={"chunks": [chunk.model_dump() for chunk in chunks]})


class PaperVisualOfTool(BaseTool):
    """沿图文互链走一步，双向通用。

    图表与讨论它的正文互为对方的证据：正文给出"我们观察到 X"，图给出 X 的量级。两边
    分开索引后，只有显式跟这条边才能把它们放到一起。
    """

    spec = ToolSpec(
        name="paper_visual_of",
        description=(
            "Follow the text/visual link one step. Given text chunk ids it returns "
            "the figures and tables those chunks discuss; given a figure or table it "
            "returns the text that discusses it. Returns chunk ids with a short "
            "opening snippet, not full text. Use it when a passage states a finding "
            "and you need the measured values behind it, or when a figure looks "
            "relevant and you need the claim it supports."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "chunk_ids": CHUNK_IDS_SCHEMA,
            },
            "required": ["corpus_ref", "chunk_ids"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self, artifacts: ArtifactStore, session: RetrievalSession | None = None
    ) -> None:
        self.artifacts = artifacts
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """走一步图文互链。"""
        corpus_ref = require_corpus_ref(input)
        chunk_ids = require_chunk_ids(input)
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        corpus = await self.session.load(self.artifacts, corpus_ref)
        hits = visual_links(corpus, chunk_ids)
        return ToolResult(data={"hits": [hit.model_dump() for hit in hits]})


class PaperCitesTool(BaseTool):
    """沿引用边走一步，正向或反向。

    反向边（谁引用了这篇）能直接给出"后续工作如何评价它"，语义检索按定义会优先返回
    与查询措辞一致、也就是同意它的文本。但这条边只在语料内部成立：一份按主题抓来的
    语料里，论文之间互相引用本来就稀疏（实测 44 篇里只有 11 篇引到了语料内的另一篇）。
    因此工具描述如实说明"多数 chunk 没有引用边"，让模型在空结果时改走章节检索，而不是
    反复重试同一个算子。
    """

    spec = ToolSpec(
        name="paper_cites",
        description=(
            "Follow citation edges one step within the corpus. direction=cites "
            "returns the in-corpus papers the given chunks cite. direction=cited_by "
            "returns the chunks elsewhere in the corpus that cite the paper those "
            "chunks belong to. Edges exist only where one corpus paper's reference "
            "list names another corpus paper, which on a topically gathered corpus "
            "is sparse — most chunks have no edge and an empty result is normal, not "
            "an error. When this returns nothing and you need evidence that "
            "qualifies or disputes a claim, use paper_section_search on Limitations "
            "or Ablation instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "chunk_ids": CHUNK_IDS_SCHEMA,
                "direction": {
                    "type": "string",
                    "enum": ["cites", "cited_by"],
                    "default": "cites",
                    "description": "Forward to cited papers, or reverse to citing chunks.",
                },
            },
            "required": ["corpus_ref", "chunk_ids"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self, artifacts: ArtifactStore, session: RetrievalSession | None = None
    ) -> None:
        self.artifacts = artifacts
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """走一步引用边。"""
        corpus_ref = require_corpus_ref(input)
        chunk_ids = require_chunk_ids(input)
        direction = input.get("direction", "cites")
        if direction not in {"cites", "cited_by"}:
            raise ValueError("direction must be 'cites' or 'cited_by'.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        corpus = await self.session.load(self.artifacts, corpus_ref)
        hits = citation_links(corpus, chunk_ids, direction)
        return ToolResult(
            data={"direction": direction, "hits": [hit.model_dump() for hit in hits]}
        )


class PaperSectionSearchTool(BaseTool):
    """按章节名跨论文取 chunk，自顶向下的结构入口。"""

    spec = ToolSpec(
        name="paper_section_search",
        description=(
            "Return chunks whose section ancestry contains the given heading, across "
            "every paper in the corpus unless paper_ids restricts it. Use it to "
            "compare the same part of many papers at once: Limitations, Ablation, "
            "Related Work, Experiments. This is the reliable way to reach evidence "
            "that qualifies or contradicts a hypothesis, because such evidence "
            "usually sits in a comparable section of a different paper and is worded "
            "unlike the hypothesis itself."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "heading": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Case-insensitive substring of a section name.",
                },
                "paper_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Restrict to these papers; omit to span the corpus.",
                },
                "k": TOP_K_SCHEMA,
            },
            "required": ["corpus_ref", "heading"],
            "additionalProperties": False,
        },
    )

    def __init__(
        self, artifacts: ArtifactStore, session: RetrievalSession | None = None
    ) -> None:
        self.artifacts = artifacts
        self.session = session or RetrievalSession()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """按章节名取 chunk。"""
        corpus_ref = require_corpus_ref(input)
        heading = input.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            raise ValueError("heading must be a non-empty string.")
        paper_ids = input.get("paper_ids") or []
        if not isinstance(paper_ids, list):
            raise ValueError("paper_ids must be an array of paper identifiers.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        corpus = await self.session.load(self.artifacts, corpus_ref)
        hits = section_search(
            corpus,
            heading,
            [item for item in paper_ids if isinstance(item, str)],
            resolve_top_k(input),
        )
        return ToolResult(data={"hits": [hit.model_dump() for hit in hits]})

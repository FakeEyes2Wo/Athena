"""paper_rag 的 Athena 工具边界：暴露给模型的三个分层检索接口。

Agent loop 复用 Athena 已有的 ``core/agent``，这里只提供接口。两个检索工具是纯读取因
而可并行；``paper_chunk_read`` 会更新会话已读集合，故按串行执行。
"""

import asyncio

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.paper_rag.interfaces import TextEmbedder
from athena.research.paper_rag.search import (
    RetrievalSession,
    keyword_search,
    read_chunks,
    semantic_search,
)
from athena.storage.artifact_store import ArtifactStore

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
CORPUS_REF_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Artifact reference of a paper corpus index built from paper_markdown output."
    ),
}
TOP_K_SCHEMA = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_TOP_K,
    "default": DEFAULT_TOP_K,
    "description": "Number of chunks to return.",
}


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
            "the chunks worth reading in full, or on their related_ids to reach the "
            "figures and tables a chunk discusses."
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
            "the full chunk. Best when the wording in the papers is unknown. Follow "
            "up with paper_chunk_read on the chunks worth reading in full, or on "
            "their related_ids to reach the figures and tables a chunk discusses."
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

        corpus = await self.session.load(self.artifacts, corpus_ref)
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

        vectors = await self.embedder.embed([query])
        hits = semantic_search(corpus, vectors[0], resolve_top_k(input))
        return ToolResult(data={"hits": [hit.model_dump() for hit in hits]})


class PaperChunkReadTool(BaseTool):
    """整篇读取指定 chunk，本会话内重复读取只返回提示。

    检索工具只给片段，全文必须显式读取——这条渐进披露让上下文只装 Agent 判断过值得
    读的内容。
    """

    spec = ToolSpec(
        name="paper_chunk_read",
        description=(
            "Read the full text of chunks returned by the search tools. Chunks "
            "already read in this session return a short notice instead of their "
            "text, so re-reading costs nothing. Set include_adjacent to also read "
            "the neighbouring chunks of the same paper for surrounding context. "
            "Each result carries related_ids: pass them back here to move between a "
            "figure or table and the text that discusses it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_ref": CORPUS_REF_SCHEMA,
                "chunk_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "description": "Chunk ids reported by a search tool.",
                },
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

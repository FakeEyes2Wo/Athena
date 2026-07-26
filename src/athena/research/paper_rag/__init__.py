"""paper_markdown 下游的 A-RAG 分层检索接口。

语料由 ``build_corpus_index`` 离线构建（一次性付出编码成本），检索则通过三个暴露给模
型的工具完成：关键词、语义、整篇读取。Agent loop 复用 Athena 已有的 ``core/agent``。
"""

from athena.research.paper_rag.index import (
    build_corpus_index,
    is_indexable,
    split_sentences,
)
from athena.research.paper_rag.interfaces import ChunkContextualizer, TextEmbedder
from athena.research.paper_rag.schemas import (
    ChunkRead,
    CorpusEntry,
    CorpusSentence,
    PaperCorpusIndex,
    SearchHit,
)
from athena.research.paper_rag.search import (
    RetrievalSession,
    keyword_search,
    read_chunks,
    semantic_search,
)
from athena.research.paper_rag.tool import (
    PaperChunkReadTool,
    PaperKeywordSearchTool,
    PaperSemanticSearchTool,
)

__all__ = [
    "PaperCorpusIndex",
    "CorpusEntry",
    "CorpusSentence",
    "SearchHit",
    "ChunkRead",
    "TextEmbedder",
    "ChunkContextualizer",
    "RetrievalSession",
    "build_corpus_index",
    "split_sentences",
    "is_indexable",
    "keyword_search",
    "semantic_search",
    "read_chunks",
    "PaperKeywordSearchTool",
    "PaperSemanticSearchTool",
    "PaperChunkReadTool",
]

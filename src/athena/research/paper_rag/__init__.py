"""paper_markdown 下游的 A-RAG 分层检索接口。

语料由 ``build_corpus_index`` 离线构建（一次性付出编码成本），检索通过暴露给模型的工具
完成：两个按内容找入口的检索算子（关键词、语义）、三个沿类型化边走一步的遍历算子（图文
互链、引用、章节），以及整篇读取。Agent loop 复用 Athena 已有的 ``core/agent``。
"""

from athena.research.paper_rag.index import (
    NonSemanticEmbedderError,
    build_corpus_index,
    require_semantic_embedder,
    semantic_margin,
    is_indexable,
    split_sentences,
)
from athena.research.paper_rag.interfaces import TextEmbedder
from athena.research.paper_rag.schemas import (
    ChunkRead,
    CorpusEntry,
    CorpusOverview,
    CorpusSentence,
    PaperCorpusIndex,
    PaperSummary,
    SearchHit,
)
from athena.research.paper_rag.search import (
    CorpusCache,
    RetrievalSession,
    citation_links,
    corpus_overview,
    corpus_paper_ids,
    keyword_search,
    read_chunks,
    section_search,
    semantic_search,
    visual_links,
)
from athena.research.paper_rag.tool import (
    PaperChunkReadTool,
    PaperCitesTool,
    PaperCorpusOverviewTool,
    PaperKeywordSearchTool,
    PaperSectionSearchTool,
    PaperSemanticSearchTool,
    PaperVisualOfTool,
)

__all__ = [
    "PaperCorpusIndex",
    "CorpusEntry",
    "CorpusOverview",
    "CorpusSentence",
    "PaperSummary",
    "SearchHit",
    "ChunkRead",
    "TextEmbedder",
    "CorpusCache",
    "RetrievalSession",
    "NonSemanticEmbedderError",
    "build_corpus_index",
    "require_semantic_embedder",
    "semantic_margin",
    "split_sentences",
    "is_indexable",
    "corpus_overview",
    "corpus_paper_ids",
    "keyword_search",
    "semantic_search",
    "section_search",
    "visual_links",
    "citation_links",
    "read_chunks",
    "PaperCorpusOverviewTool",
    "PaperKeywordSearchTool",
    "PaperSemanticSearchTool",
    "PaperSectionSearchTool",
    "PaperVisualOfTool",
    "PaperCitesTool",
    "PaperChunkReadTool",
]

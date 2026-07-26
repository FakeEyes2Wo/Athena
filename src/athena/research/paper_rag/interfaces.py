"""paper_rag 所需的供应商无关模型协议。"""

from typing import Protocol


class TextEmbedder(Protocol):
    """把文本批量编码为稠密向量的接口。

    ``model`` 会写进语料索引：语义检索要求建索引与查询使用同一模型，索引里记下模型
    标识，换模型后才能在查询时立刻发现不一致，而不是返回一批无意义的相似度。
    """

    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码文本，返回与输入等长、顺序一致的向量列表。"""


class ChunkContextualizer(Protocol):
    """给 chunk 生成上下文前缀的接口，供 Contextual Retrieval 备选方案使用。

    当前没有任何实现，索引链路也不调用它；保留契约是为了让备选方案落地时不必再改
    ``build_corpus_index`` 的签名。设计背景见 ``docs/paper_rag_tool_ch.md``。
    """

    model: str

    async def contextualize(self, document: str, chunks: list[str]) -> list[str]:
        """为每个 chunk 生成一段定位它在全文中位置的前缀，顺序与输入一致。"""

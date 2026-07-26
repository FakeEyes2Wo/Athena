"""paper_rag 语义检索所需的供应商无关文本编码协议。"""

from typing import Protocol


class TextEmbedder(Protocol):
    """把文本批量编码为稠密向量的接口。

    ``model`` 会写进语料索引：语义检索要求建索引与查询使用同一模型，索引里记下模型
    标识，换模型后才能在查询时立刻发现不一致，而不是返回一批无意义的相似度。
    """

    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码文本，返回与输入等长、顺序一致的向量列表。"""

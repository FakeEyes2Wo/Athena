"""paper_rag 所需的供应商无关模型协议。"""

from typing import TYPE_CHECKING, Protocol

import numpy

if TYPE_CHECKING:
    from athena.research.literature.paper_markdown.models import PaperContent


class TextEmbedder(Protocol):
    """把文本批量编码为稠密向量的接口。

    ``model`` 会写进语料索引：语义检索要求建索引与查询使用同一模型，索引里记下模型
    标识，换模型后才能在查询时立刻发现不一致，而不是返回一批无意义的相似度。
    """

    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码文本，返回与输入等长、顺序一致的向量列表。"""


class VectorCache(Protocol):
    """按论文缓存句向量的接口。

    以论文为粒度而不是以句子为粒度：句子是从 ``PaperContent`` 确定性切出来的，同一篇
    论文在两份语料里切出的序列必然相同，因此整篇复用是安全的，而按句缓存要为每条句子
    各存一次键，得不偿失。

    实现方负责校验条数——``load`` 拿回的矩阵行数必须等于 ``end - start``，对不上就该
    返回 ``None`` 让调用方重新编码。向量与句子的一一对应是语义检索唯一的正确性前提：
    错位不会报错，只会让之后每一次检索都返回错的句子。
    """

    async def load(
        self, paper: "PaperContent", start: int, end: int
    ) -> numpy.ndarray | None:
        """取回该论文的句向量矩阵；未缓存或条数不符时返回 ``None``。"""

    async def save(self, paper: "PaperContent", vectors: numpy.ndarray) -> None:
        """存下该论文的句向量矩阵。"""

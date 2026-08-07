"""A-RAG 分层语料索引与三个检索接口的返回模型。

索引拆成两个 artifact：``PaperCorpusIndex`` 本体带正文与句子区间，句向量单独落盘。
关键词检索与整篇读取因此完全不需要加载向量，这也是语料未建向量时仍可工作的原因。
"""

from typing import Literal

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef


class CorpusSentence(BaseModel):
    """句级检索单元，以父 chunk 内的字符区间存储，避免正文在索引里重复落盘。"""

    entry_index: int = Field(ge=0, description="Owning entry position in the index.")
    char_start: int = Field(ge=0, description="Inclusive offset in the entry text.")
    char_end: int = Field(ge=0, description="Exclusive offset in the entry text.")


class CorpusEntry(BaseModel):
    """一个可被检索和整篇读取的 chunk，来自上游 ``RetrievalUnit``。"""

    chunk_id: str = Field(description="Paper-namespaced retrieval unit identifier.")
    paper_id: str = Field(default="", description="Canonical upstream paper id.")
    title: str = Field(default="", description="Paper title for agent orientation.")
    kind: str = Field(description="Retrieval unit type such as paragraph or visual.")
    heading_path: list[str] = Field(
        default_factory=list, description="Section ancestry used for orientation."
    )
    text: str = Field(description="Full retrieval text of this chunk.")
    related_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Identifiers of directly related units: the visuals a text chunk "
            "discusses, or the text chunks that discuss a visual."
        ),
    )
    sentence_start: int = Field(
        ge=0, description="Inclusive index of this entry's first sentence."
    )
    sentence_end: int = Field(
        ge=0, description="Exclusive index of this entry's last sentence."
    )


class PaperCorpusIndex(BaseModel):
    """A-RAG 三级语料：隐式关键词层、句级向量层、chunk 级全文层。

    关键词层刻意不预建倒排索引，查询时直接扫描 ``entries`` 的正文；因此换语料只需重
    新编码句子，不存在需要维护的离线图或倒排结构。
    """

    schema_version: Literal["1.0"] = Field(
        default="1.0", description="PaperCorpusIndex schema version."
    )
    entries: list[CorpusEntry] = Field(description="Ordered chunks, grouped by paper.")
    sentences: list[CorpusSentence] = Field(
        description="Sentence spans ordered to match the persisted embedding vectors."
    )
    embedding_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact holding one unit-length vector per sentence; absent when no embedder was supplied.",
    )
    embedding_model: str = Field(
        default="",
        description="Embedder identity; queries must use the same model as the index.",
    )

    def sentence_text(self, position: int) -> str:
        """取第 ``position`` 个句子的原文，例如 ``index.sentence_text(0)``。"""
        sentence = self.sentences[position]
        entry = self.entries[sentence.entry_index]
        return entry.text[sentence.char_start : sentence.char_end]


class SearchHit(BaseModel):
    """一条检索命中。

    只带 snippet 而不带全文，Agent 据此决定是否值得对该 chunk 发起 ``chunk_read``；
    这条渐进披露正是 A-RAG 相对朴素 agentic 检索省下大量上下文的原因。
    """

    chunk_id: str = Field(description="Identifier accepted by paper_chunk_read.")
    paper_id: str = Field(default="", description="Canonical upstream paper id.")
    title: str = Field(default="", description="Paper title.")
    kind: str = Field(description="Retrieval unit type.")
    heading_path: list[str] = Field(
        default_factory=list, description="Section ancestry of the chunk."
    )
    score: float = Field(description="Channel-specific relevance score.")
    snippet: str = Field(description="Matched sentences only, never the full chunk.")
    related_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Related figure/table or text chunk ids, ready to pass to paper_chunk_read."
        ),
    )


class ChunkRead(BaseModel):
    """一次整篇读取的结果。

    ``already_read`` 表示本会话内已经读过：此时 ``text`` 是一句提示而非正文，重复读取
    因而不再消耗上下文。
    """

    chunk_id: str = Field(description="Requested or adjacent chunk identifier.")
    status: Literal["read", "already_read", "not_found"] = Field(
        description="Whether full text is returned, suppressed, or unknown."
    )
    title: str = Field(default="", description="Paper title.")
    heading_path: list[str] = Field(
        default_factory=list, description="Section ancestry of the chunk."
    )
    text: str = Field(default="", description="Full chunk text or a short notice.")
    related_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Related figure/table or text chunk ids, ready to pass to paper_chunk_read."
        ),
    )

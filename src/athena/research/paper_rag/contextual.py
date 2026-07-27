"""Contextual Retrieval 备选方案的骨架，当前不接入检索链路。

已实现的结构感知切句（``index.is_indexable``）解决的是"结构片段不该参与检索"；它解决
不了另一半问题——正文 chunk 本身也可能缺少自足语境，例如"该变体在两个数据集上均下降
1.2 分"这样的句子，脱离所属实验设置后既无法被检索到，也无法被读懂。

Contextual Retrieval（Anthropic, 2024）的做法是：在编码之前，由一个模型为每个 chunk 生
成一段说明它在全文中位置与角色的前缀，把前缀与正文拼接后再嵌入。代价是每篇论文一次
额外的模型调用，因此它是备选而不是默认。

此模块只声明入口与契约，不含可用实现：Athena 目前没有配置任何 ``ChunkContextualizer``，
把半成品接进 ``build_corpus_index`` 只会让语料在无声中变成两种不兼容的形态。真正落地时
需要一并决定的问题记在 ``docs/paper_rag_tool_ch.md`` 的"备选方案"一节。
"""

from athena.research.paper_rag.interfaces import ChunkContextualizer
from athena.research.paper_rag.schemas import CorpusEntry


async def contextualize_entries(
    entries: list[CorpusEntry],
    document: str,
    contextualizer: ChunkContextualizer,
) -> list[CorpusEntry]:
    """把上下文前缀写进每个 chunk 的检索文本，返回新的条目列表。

    尚未实现：调用它会抛 ``NotImplementedError``，而不是悄悄返回原样条目——后者会让
    调用方以为语料已经过上下文增强。
    """
    raise NotImplementedError(
        "Contextual Retrieval is a documented alternative, not an implemented path; "
        "see docs/paper_rag_tool_ch.md before wiring it into build_corpus_index."
    )

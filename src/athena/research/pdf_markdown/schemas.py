"""可寻址论文 Markdown 的事实模型与其持久化投影。

一篇论文 PDF 经 markitdown 逐页转换后得到两种表示，二者共享同一套可寻址**索引**
（``PaperIndex``：页码区间、章节标题、图/表题注、图表内联引用）：

- ``PaperMarkdown``：转换即时产物，整篇 markdown 与逐章节文本以**内联字符串**给出，
  便于在内存中直接使用；
- ``PaperContent``：落库投影，整篇 markdown 只存一份为 ``ArtifactRef``，索引随之内联
  保留，**持久化后仍可直接查询**页码/章节/图表；逐章节文本按需从整篇 markdown 切片
  重建（见 ``load_sections``），不重复落盘。

设计约束：大对象（整篇 markdown）以 ``ArtifactRef`` 保存；小型结构化索引内联且可查询；
转换器名称与源文件指纹随产物记录，便于全局缓存与日后整体替换转换器重建。所有 Pydantic
``description`` 使用英文。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from athena.core.schemas import ArtifactRef

if TYPE_CHECKING:  # 仅类型检查期引入存储接口，避免数据模型在运行时耦合存储层。
    from athena.storage.artifact_store import ArtifactStore


# ====== 导入的包 ======
# 首个标题之前内容（标题页/摘要区）的章节键。
PREAMBLE_KEY = "(preamble)"
# 全文无任何标题时的整篇章节键。
DOCUMENT_KEY = "(document)"
# ======


class PageSpan(BaseModel):
    """整篇 markdown 中一页所占的字符区间，用于按偏移反查页码。"""

    page_number: int = Field(description="1-based page number in the source PDF.")
    char_start: int = Field(description="Inclusive start offset of this page in the full markdown.")
    char_end: int = Field(description="Exclusive end offset of this page in the full markdown.")


class Heading(BaseModel):
    """一个被识别的章节标题，及其在全文中的定位与层级路径。"""

    level: int = Field(description="Heading depth; 1 is a top-level section.")
    title: str = Field(description="Heading text as extracted by markitdown.")
    path: str = Field(description="Slash-joined ancestor titles, e.g. '4 Methodology / 4.1 Overview'.")
    page_number: int = Field(description="Page where the heading starts.")
    char_offset: int = Field(description="Start offset of the heading line in the full markdown.")
    anchor: str = Field(description="Stable in-document anchor, e.g. 'sec-4-1-overview'.")


class FloatRef(BaseModel):
    """一个图或表的题注（float），及其定位与锚点。"""

    kind: Literal["figure", "table"] = Field(description="Whether the float is a figure or a table.")
    label: str = Field(description="Normalized label, e.g. 'Figure 1' or 'Table 2'.")
    number: str = Field(description="Caption number as text, e.g. '1'.")
    caption: str = Field(description="Full caption line text.")
    page_number: int = Field(description="Page where the caption appears.")
    char_offset: int = Field(description="Start offset of the caption line in the full markdown.")
    anchor: str = Field(description="Stable in-document anchor, e.g. 'fig-1' or 'tab-2'.")


class CrossReference(BaseModel):
    """正文中对某图/表的一次内联引用（题注本身不计入）。"""

    label: str = Field(description="Referenced label as written, e.g. 'Figure 1'.")
    target_anchor: str = Field(description="Anchor of the matching float, or '' if unresolved.")
    page_number: int = Field(description="Page where the reference occurs.")
    char_offset: int = Field(description="Start offset of the reference in the full markdown.")


def page_of_offset(pages: list[PageSpan], char_offset: int) -> int:
    """在页码区间表中定位给定偏移所属页码；越界时取最近的合法页码。

    示例：page_of_offset([PageSpan(page_number=1, char_start=0, char_end=10)], 3) == 1
    """
    # 逐段线性扫描；论文页数通常只有数十页，无需二分。
    for span in pages:
        if span.char_start <= char_offset < span.char_end:
            return span.page_number
    return pages[-1].page_number if pages else 1


def section_bounds(headings: list[Heading], doc_end: int) -> list[tuple[str, int, int]]:
    """由标题偏移推出各章节的 (path, char_start, char_end)，含首个标题前的 preamble。

    章节即相邻标题之间的区间，故只需标题偏移即可界定，无需另存章节文本。
    示例：section_bounds([Heading(path='1 Intro', char_offset=5, ...)], 20)
          == [('1 Intro', 5, 20)]
    """
    bounds: list[tuple[str, int, int]] = []
    if headings and headings[0].char_offset > 0:
        bounds.append((PREAMBLE_KEY, 0, headings[0].char_offset))
    for index, head in enumerate(headings):
        end = headings[index + 1].char_offset if index + 1 < len(headings) else doc_end
        bounds.append((head.path, head.char_offset, end))
    return bounds


def build_sections(markdown: str, headings: list[Heading]) -> dict[str, str]:
    """按标题偏移把整篇 markdown 切成"章节路径 -> 章节文本"，供下游 RAG 分块。

    首个标题之前的内容归入 ``(preamble)``；无任何标题时整篇归入 ``(document)``。
    示例：build_sections(md, headings) 的键包含各标题的 path。
    """
    if not headings:
        return {DOCUMENT_KEY: markdown.strip()}
    sections: dict[str, str] = {}
    for path, start, end in section_bounds(headings, len(markdown)):
        # 同名 path 罕见冲突时用起始偏移消歧，保证键唯一。
        key = path if path not in sections else f"{path} [{start}]"
        sections[key] = markdown[start:end].strip()
    return sections


class PaperIndex(BaseModel):
    """一篇论文的可寻址索引，被内存态与落库态共享。

    仅含小型结构化事实（页码区间、章节标题、图/表题注、图表引用）与其在整篇 markdown
    中的字符偏移；不含整篇/逐章节大文本。据此可在**不加载正文**的情况下查询页码、章节
    结构与图表位置。
    """

    fingerprint: str = Field(description="SHA-256 hex digest of the source PDF; global cache key.")
    converter: str = Field(description="Converter name and version, e.g. 'markitdown-0.1.6'.")
    page_count: int = Field(description="Number of pages in the source PDF.")
    pages: list[PageSpan] = Field(description="Per-page character spans within the full markdown.")
    headings: list[Heading] = Field(description="Detected section headings in document order.")
    figures: list[FloatRef] = Field(default_factory=list, description="Figure captions with anchors.")
    tables: list[FloatRef] = Field(default_factory=list, description="Table captions with anchors.")
    cross_references: list[CrossReference] = Field(default_factory=list, description="Inline figure/table references.")

    def page_of(self, char_offset: int) -> int:
        """返回给定字符偏移所在的页码；越界时取最近的合法页码。"""
        return page_of_offset(self.pages, char_offset)

    def find_float(self, label: str) -> FloatRef | None:
        """按标签查图/表题注，如 find_float('Figure 1')；未命中返回 None。"""
        for item in (*self.figures, *self.tables):
            if item.label == label:
                return item
        return None

    def section_spans(self) -> list[tuple[str, int, int]]:
        """返回各章节的 (path, char_start, char_end)，供纯索引查询与切片，无需正文。"""
        doc_end = self.pages[-1].char_end if self.pages else 0
        return section_bounds(self.headings, doc_end)


class PaperContent(PaperIndex):
    """落库投影：整篇 markdown 存为一份 artifact，可寻址索引内联保留、持久化后可直接查询。

    作为 design.md 中 ``PaperRecord.content`` 的取值：只对判定 relevant 的论文生成，交付
    下游 Paper Reading RAG 前必须就绪。逐章节文本不单独落盘，按需由 ``load_sections`` 从
    整篇 markdown 切片重建（章节边界已由内联的 ``headings`` 界定），避免重复存储。
    """

    markdown_ref: ArtifactRef = Field(description="Artifact holding the full addressable markdown.")

    async def load_markdown(self, store: "ArtifactStore") -> str:
        """从 artifact 取回整篇 markdown。"""
        return await store.get_text(self.markdown_ref)

    async def load_sections(self, store: "ArtifactStore") -> dict[str, str]:
        """取回整篇 markdown 并切成"章节路径 -> 章节文本"，作为下游 RAG 分块输入。

        示例：sections = await content.load_sections(store); sections['1 Introduction']
        """
        return build_sections(await self.load_markdown(store), self.headings)


class PaperMarkdown(PaperIndex):
    """转换即时产物：可寻址索引 + 内联整篇 markdown + 内联逐章节文本。

    输入：单篇论文 PDF。输出：整篇 markdown（含页码/章节/图表锚点）、页码区间、章节标题、
    逐章节文本（供下游 RAG 分块）、图与表题注，以及正文中的图表内联引用。
    """

    markdown: str = Field(description="Full addressable markdown with page, section and float anchors.")
    sections: dict[str, str] = Field(description="Heading path -> section markdown; direct RAG chunking input.")

    async def persist(self, store: "ArtifactStore") -> PaperContent:
        """把整篇 markdown 存为一份 artifact，返回携带内联索引的 ``PaperContent``。

        章节文本不单独落盘（可由索引切片重建）；整篇 markdown 内容寻址，跨 Session 去重。
        示例：content = await paper_markdown.persist(store); await content.load_markdown(store)
        """
        markdown_ref = await store.put_text(self.markdown)
        shared = {name: getattr(self, name) for name in PaperIndex.model_fields}
        return PaperContent(markdown_ref=markdown_ref, **shared)

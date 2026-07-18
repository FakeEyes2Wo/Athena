"""可寻址论文 Markdown 的事实模型。

本模块描述一篇论文 PDF 经 markitdown 逐页转换后的结构化产物：整篇 markdown 保留
页码锚点、章节锚点与图表锚点，可按字符偏移反查页码、章节与图表，供下游 RAG 分块
与"带页码/章节"的证据引用。

设计约束（对应 design.md 的 Markdownify 阶段）：
- 大文本（整篇 markdown 与逐章节文本）在本层以内联字符串给出，落库时由上层改写为
  不可变 ``ArtifactRef``；
- 转换器名称与源文件指纹随产物一并记录，便于全局缓存与日后整体替换转换器重建；
- 所有 Pydantic ``description`` 使用英文。
"""

from typing import Literal

from pydantic import BaseModel, Field


# ====== 导入的包 ======
# 本模块无模块级常量。
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


class PaperMarkdown(BaseModel):
    """PDF 经 markitdown 转换后的可寻址 markdown 产物。

    输入：单篇论文 PDF。
    输出：整篇 markdown（含页码/章节/图表锚点）、页码区间表、章节标题表、逐章节
    文本（供下游 RAG 分块）、图与表题注表，以及正文中的图表内联引用表。
    """

    fingerprint: str = Field(description="SHA-256 hex digest of the source PDF; global cache key.")
    converter: str = Field(description="Converter name and version, e.g. 'markitdown-0.1.6'.")
    page_count: int = Field(description="Number of pages in the source PDF.")
    markdown: str = Field(description="Full addressable markdown with page, section and float anchors.")
    pages: list[PageSpan] = Field(description="Per-page character spans within the full markdown.")
    headings: list[Heading] = Field(description="Detected section headings in document order.")
    sections: dict[str, str] = Field(description="Heading path -> section markdown; direct RAG chunking input.")
    figures: list[FloatRef] = Field(default_factory=list, description="Figure captions with anchors.")
    tables: list[FloatRef] = Field(default_factory=list, description="Table captions with anchors.")
    cross_references: list[CrossReference] = Field(default_factory=list, description="Inline figure/table references.")

    def page_of(self, char_offset: int) -> int:
        """返回给定字符偏移所在的页码；越界时取最近的合法页码。

        示例：pm.page_of(pm.headings[0].char_offset) == pm.headings[0].page_number
        """
        return page_of_offset(self.pages, char_offset)


def page_of_offset(pages: list[PageSpan], char_offset: int) -> int:
    """在页码区间表中定位给定偏移所属页码；越界时取最近的合法页码。

    示例：page_of_offset([PageSpan(page_number=1, char_start=0, char_end=10)], 3) == 1
    """
    # 逐段线性扫描；论文页数通常只有数十页，无需二分。
    for span in pages:
        if span.char_start <= char_offset < span.char_end:
            return span.page_number
    return pages[-1].page_number if pages else 1

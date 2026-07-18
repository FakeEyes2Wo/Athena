"""借助 markitdown 的 PDF -> 可寻址 Markdown 工具。

对外主入口为 ``pdf_to_markdown``：输入一篇论文 PDF，输出保留页码、章节与图表引用的
可寻址 ``PaperMarkdown``。用于 AcademicSurvey 的 Markdownify 阶段与下游 Paper Reading
RAG 的分块输入。
"""

from athena.research.pdf_markdown.converter import pdf_to_markdown
from athena.research.pdf_markdown.schemas import (
    CrossReference,
    FloatRef,
    Heading,
    PageSpan,
    PaperContent,
    PaperIndex,
    PaperMarkdown,
)

__all__ = [
    "pdf_to_markdown",
    "PaperMarkdown",
    "PaperContent",
    "PaperIndex",
    "PageSpan",
    "Heading",
    "FloatRef",
    "CrossReference",
]

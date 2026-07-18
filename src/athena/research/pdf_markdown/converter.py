"""借助 microsoft markitdown 把论文 PDF 转成可寻址 Markdown。

设计目标（对应 design.md 中 AcademicSurvey 的 Markdownify 阶段）：
- 转换器固定为 markitdown，**逐页**转换以保留页码：markitdown 整篇转换会把页边界
  丢掉，故用 pypdf 把 PDF 拆成单页流后逐页交给 markitdown；
- 页码、章节与图表以锚点内联到 markdown，并以字符偏移建立可寻址索引，供下游 RAG
  分块与"带页码/章节"的证据引用；
- 转换结果以源文件 SHA-256 指纹为键，可选地全局缓存（论文不可变，跨 Session 复用）。

模块输入输出：
- 输入：一篇论文 PDF 的路径（可选缓存目录）。
- 输出：一个 ``PaperMarkdown``（见 schemas.py），含整篇 markdown 与页码/章节/图表索引。

markitdown 对双栏版面与公式的还原偏弱（题注、标题偶有词间空格丢失或错行），本模块
只在其输出之上叠加可寻址结构，不改写正文文字，保证溯源，且日后可整体替换转换器重建。
"""

import hashlib
import io
import os
import re
import tempfile
from importlib.metadata import version
from typing import NamedTuple

from markitdown import MarkItDown
from pypdf import PdfReader, PdfWriter

from athena.research.pdf_markdown.schemas import (
    CrossReference,
    FloatRef,
    Heading,
    PageSpan,
    PaperMarkdown,
    build_sections,
    page_of_offset,
)


# ====== 导入的包 ======

# 带编号的章节标题：如 "4"、"4.1"、"5.2.1"，编号后跟以字母开头、不含表格竖线的标题。
NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+([A-Za-z][^|]{0,79})$")

# 无编号但常见的章节关键词（去掉非字母、转小写后比较）：markitdown 常吞掉词间空格，
# 故用归一化后的等值匹配，避免 "References retrieval ..." 这类误命中。
KEYWORD_HEADINGS = {
    "abstract", "introduction", "background", "relatedwork", "method", "methods",
    "methodology", "approach", "experiments", "experiment", "experimentalsetup",
    "experimentalsetting", "results", "evaluation", "discussion", "conclusion",
    "conclusions", "references", "acknowledgments", "acknowledgements", "appendix",
    "limitations", "broaderimpact",
}

# 图/表题注：行首形如 "Figure 1:"、"Fig. 2."、"Table 3:"。
FLOAT_CAPTION = re.compile(r"^(figure|fig|table)\.?\s*(\d+)\s*[:.]", re.IGNORECASE)

# 正文内联引用：如 "Figure 1"、"Fig. 2"、"Table 3"；题注也会命中，后续按偏移排除。
FLOAT_REFERENCE = re.compile(r"\b(figure|fig|table)\.?\s*(\d+)", re.IGNORECASE)

# markitdown 单页转换时声明的文件扩展名。
PDF_EXTENSION = ".pdf"

# ======


class LineTag(NamedTuple):
    """``classify_line`` 的结构化返回：一行的语义类别与要素。"""

    kind: str     # "heading" | "figure" | "table"
    level: int    # 章节层级（图表恒为 0）
    number: str   # 图表编号（章节为空串）
    text: str     # 规整（strip）后的整行文本


def slugify(text: str) -> str:
    """把标题或标签转成锚点 slug；全部转小写，非字母数字压成单个连字符。

    示例：slugify("4.1 Overview") == "4-1-overview"
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "x"


def classify_line(line: str) -> LineTag | None:
    """判定一行是否为章节标题或图表题注，都不是则返回 None。

    示例：
        classify_line("4.1 Overview").kind == "heading"
        classify_line("Figure 1: Arch").kind == "figure"
        classify_line("plain body text") is None
    """
    stripped = line.strip()
    if not stripped:
        return None

    caption = FLOAT_CAPTION.match(stripped)
    # 题注需带实质说明文字，排除 "Table14." 这类正文引用被误判为题注。
    if caption and len(stripped[caption.end():].strip()) >= 3:
        kind = "table" if caption.group(1).lower() == "table" else "figure"
        return LineTag(kind=kind, level=0, number=caption.group(2), text=stripped)

    numbered = NUMBERED_HEADING.match(stripped)
    # 标题不会以断行连字符结尾，也不含句内 ". "，据此排除 "50 instances. On average…" 这类正文。
    if numbered and not numbered.group(2).endswith("-") and ". " not in numbered.group(2):
        level = min(numbered.group(1).count(".") + 1, 6)
        return LineTag(kind="heading", level=level, number="", text=stripped)

    normalized = re.sub(r"[^a-z]", "", stripped.lower())
    if normalized in KEYWORD_HEADINGS and len(stripped) <= 40:
        return LineTag(kind="heading", level=1, number="", text=stripped)

    return None


def push_heading(stack: list[tuple[int, str]], level: int, title: str) -> str:
    """更新标题层级栈并返回当前标题的层级路径（会就地修改 stack）。

    示例：push_heading([(1, "4 Methodology")], 2, "4.1 Overview") == "4 Methodology / 4.1 Overview"
    """
    # 弹出层级不低于当前标题的祖先，再压入当前标题，栈中即为祖先链。
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))
    return " / ".join(node_title for _, node_title in stack)


def unique_anchor(base: str, used: set[str]) -> str:
    """返回不与 used 冲突的锚点，必要时追加 -2、-3……，并登记到 used。

    示例：unique_anchor("fig-1", {"fig-1"}) == "fig-1-2"
    """
    anchor = base
    suffix = 2
    while anchor in used:
        anchor = f"{base}-{suffix}"
        suffix += 1
    used.add(anchor)
    return anchor


def fingerprint_pdf(pdf_path: str) -> str:
    """返回 PDF 文件内容的 SHA-256 十六进制指纹，用作全局缓存键。

    示例：fingerprint_pdf("paper.pdf") -> "3f5a…"（64 位十六进制）
    """
    with open(pdf_path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def convert_pdf_pages(pdf_path: str) -> list[str]:
    """逐页调用 markitdown，返回每页 markdown 文本（下标 0 即第 1 页）。

    逐页转换是为了保留页码：markitdown 整篇转换会把页边界丢掉。
    示例：convert_pdf_pages("2page.pdf") -> ["page1 md", "page2 md"]
    """
    reader = PdfReader(pdf_path)
    engine = MarkItDown()
    page_texts: list[str] = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        buffer.seek(0)
        page_texts.append(engine.convert_stream(buffer, file_extension=PDF_EXTENSION).markdown)
    return page_texts


def find_cross_references(
    markdown: str, pages: list[PageSpan], floats: list[FloatRef]
) -> list[CrossReference]:
    """扫描正文中对图/表的内联引用（排除题注本身），并解析到对应锚点。

    示例：正文 "see Figure 1" 会产出一条 target_anchor 指向 "fig-1" 的 CrossReference。
    """
    caption_offsets = {item.char_offset for item in floats}
    anchor_by_key = {(item.kind, item.number): item.anchor for item in floats}
    references: list[CrossReference] = []
    for match in FLOAT_REFERENCE.finditer(markdown):
        start = match.start()
        if start in caption_offsets:
            continue  # 题注本身，不算引用
        kind = "table" if match.group(1).lower() == "table" else "figure"
        references.append(
            CrossReference(
                label=f"{kind.capitalize()} {match.group(2)}",
                target_anchor=anchor_by_key.get((kind, match.group(2)), ""),
                page_number=page_of_offset(pages, start),
                char_offset=start,
            )
        )
    return references


def build_paper_markdown(page_texts: list[str], fingerprint: str, converter: str) -> PaperMarkdown:
    """把逐页 markdown 组装成可寻址整篇 markdown，并抽取页码/章节/图表索引。

    - 每页前插入 ``<!-- page:N -->`` 页码锚点；
    - 章节标题提升为 markdown 标题并追加 ``<!-- sec-… p:N -->`` 锚点；
    - 图表题注追加 ``<!-- fig-…/tab-… p:N -->`` 锚点。
    正文文字保持 markitdown 原样，仅叠加锚点，保证可溯源。

    示例：build_paper_markdown(["1 Intro\\nbody", "Figure 1: x"], "fp", "markitdown-0.1.6")
    """
    out_lines: list[str] = []
    pages: list[PageSpan] = []
    headings: list[Heading] = []
    figures: list[FloatRef] = []
    tables: list[FloatRef] = []
    used_anchors: set[str] = set()
    path_stack: list[tuple[int, str]] = []
    offset = 0  # 已写入 out_lines 的字符数（含每行之间的换行）

    def emit(text: str) -> int:
        """写入一行，返回该行起始偏移，并推进 offset（含行间换行）。"""
        nonlocal offset
        start = offset
        out_lines.append(text)
        offset += len(text) + 1
        return start

    for page_index, page_text in enumerate(page_texts):
        page_number = page_index + 1
        page_start = offset
        emit(f"<!-- page:{page_number} -->")
        for line in page_text.split("\n"):
            tag = classify_line(line)
            if tag is None:
                emit(line)
            elif tag.kind == "heading":
                path = push_heading(path_stack, tag.level, tag.text)
                anchor = unique_anchor("sec-" + slugify(tag.text), used_anchors)
                head_offset = emit(f"{'#' * tag.level} {tag.text}  <!-- {anchor} p:{page_number} -->")
                headings.append(Heading(level=tag.level, title=tag.text, path=path,
                                         page_number=page_number, char_offset=head_offset, anchor=anchor))
            else:
                anchor = unique_anchor(("fig-" if tag.kind == "figure" else "tab-") + tag.number, used_anchors)
                float_offset = emit(f"{tag.text}  <!-- {anchor} p:{page_number} -->")
                bucket = figures if tag.kind == "figure" else tables
                bucket.append(FloatRef(kind=tag.kind, label=f"{tag.kind.capitalize()} {tag.number}",
                                       number=tag.number, caption=tag.text, page_number=page_number,
                                       char_offset=float_offset, anchor=anchor))
        pages.append(PageSpan(page_number=page_number, char_start=page_start, char_end=offset))

    markdown = "\n".join(out_lines)
    return PaperMarkdown(
        fingerprint=fingerprint,
        converter=converter,
        page_count=len(page_texts),
        markdown=markdown,
        pages=pages,
        headings=headings,
        sections=build_sections(markdown, headings),
        figures=figures,
        tables=tables,
        cross_references=find_cross_references(markdown, pages, figures + tables),
    )


def pdf_to_markdown(pdf_path: str, cache_dir: str | None = None) -> PaperMarkdown:
    """把一篇论文 PDF 转成可寻址 Markdown（本模块的对外主入口）。

    传入 cache_dir 时按源文件指纹全局缓存（论文不可变，跨 Session 复用）：命中则直接
    读回，未命中则转换后写入 ``{cache_dir}/{fingerprint}.json``。

    示例：pdf_to_markdown("PaSa.pdf").page_count -> 17
    """
    fingerprint = fingerprint_pdf(pdf_path)
    cache_file = os.path.join(cache_dir, f"{fingerprint}.json") if cache_dir else None
    if cache_file and os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as handle:
            return PaperMarkdown.model_validate_json(handle.read())

    converter = f"markitdown-{version('markitdown')}"
    result = build_paper_markdown(convert_pdf_pages(pdf_path), fingerprint, converter)

    if cache_file:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as handle:
            handle.write(result.model_dump_json(indent=2))
    return result


def pdf_bytes_to_markdown(pdf_bytes: bytes, cache_dir: str | None = None) -> PaperMarkdown:
    """把内存中的 PDF 字节转成可寻址 Markdown。

    供以 artifact 交接的调用方（如工具适配器）使用：先把字节写入临时文件，再复用
    ``pdf_to_markdown``，转换完成即删除临时文件。
    示例：pdf_bytes_to_markdown(open("p.pdf", "rb").read()).page_count -> 17
    """
    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    handle.write(pdf_bytes)
    handle.close()
    try:
        return pdf_to_markdown(handle.name, cache_dir=cache_dir)
    finally:
        os.remove(handle.name)

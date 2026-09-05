"""PyMuPDF 回退通道：从 PDF 版面恢复适合 RAG 的 Markdown 和视觉资源。"""

import hashlib
import re
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pymupdf as fitz

from athena.research.literature.contracts import ProcessingDiagnostic
from athena.research.literature.paper_markdown.models import (
    VISUAL_TOKEN,
    ParsedElement,
    ParsedPaper,
    ParsedVisual,
    SourceLocator,
)
from athena.research.literature.paper_markdown.pdf_elements import (
    PdfBlock,
    citation_keys,
    display_reference_keys,
    markdown_table,
    reading_order,
)
from athena.research.literature.paper_markdown.visuals import render_page_region

CAPTION_PATTERN = re.compile(
    r"^(?P<kind>figure|fig\.?|table)\s*(?P<number>[A-Za-z]?\d+(?:[.-]\d+)*)\s*[:.]\s*(?P<caption>.*)$",
    re.IGNORECASE,
)
NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+){0,4}|[A-Z])\.?\s+[A-Z][^.!?]{0,120}$")
KEYWORD_HEADINGS = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "method",
    "methods",
    "methodology",
    "approach",
    "experiments",
    "experimental setup",
    "results",
    "evaluation",
    "discussion",
    "conclusion",
    "conclusions",
    "references",
    "acknowledgments",
    "acknowledgements",
    "appendix",
    "limitations",
}
UNINDEXABLE_PDF_CHARACTER = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|[\ue000-\uf8ff]"
)
APPENDIX_SUBHEADING = re.compile(r"^([A-Z]\.\d+(?:\.\d+){0,3})\.?\s+")
NUMBERED_EQUATION = re.compile(r"\(\d+\)\s*$")
BIBLIOGRAPHY_YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\.")
MIN_VISUAL_AREA_RATIO = 0.008


def round_bbox(values: Iterable[float]) -> tuple[float, float, float, float]:
    """把 PyMuPDF 坐标规范到两位小数。"""
    x0, y0, x1, y1 = values
    return round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)


def join_pdf_lines(lines: list[str]) -> str:
    """连接 PDF 行并修复确定性的断词连字符。"""
    result = ""
    for line in (value.strip() for value in lines if value.strip()):
        if not result:
            result = line
        elif result.endswith("-") and line[:1].islower():
            result = result[:-1] + line
        elif result.endswith(("/", "(", "[")):
            result += line
        else:
            result += " " + line
    return re.sub(r"\s+", " ", result).strip()


def text_from_block(raw: dict[str, Any]) -> tuple[str, float, bool, list[float], int]:
    """从 PyMuPDF dict block 提取文本、最大字号、粗体和逐 span 字号。"""
    lines: list[str] = []
    sizes: list[float] = []
    bold = False
    for line in raw.get("lines", []):
        fragments: list[str] = []
        for span in line.get("spans", []):
            value = UNINDEXABLE_PDF_CHARACTER.sub("", str(span.get("text", "")))
            if value:
                fragments.append(value)
            size = float(span.get("size", 0.0))
            if value.strip() and size > 0:
                sizes.extend([size] * max(1, len(value.strip())))
            font = str(span.get("font", "")).lower()
            flags = int(span.get("flags", 0))
            bold = bold or "bold" in font or bool(flags & 16)
        if fragments:
            lines.append("".join(fragments))
    return join_pdf_lines(lines), max(sizes, default=0.0), bold, sizes, len(lines)


def extract_page_blocks(page: fitz.Page) -> tuple[list[PdfBlock], list[float]]:
    """读取一页的文本与嵌入图片块。"""
    result: list[PdfBlock] = []
    font_sizes: list[float] = []
    page_dict = page.get_text("dict", sort=False)
    for index, raw in enumerate(page_dict.get("blocks", [])):
        bbox = round_bbox(raw.get("bbox", (0, 0, 0, 0)))
        if raw.get("type") == 0:
            text, max_size, bold, sizes, line_count = text_from_block(raw)
            if not text:
                continue
            font_sizes.extend(sizes)
            result.append(
                PdfBlock(
                    page_number=page.number + 1,
                    bbox=bbox,
                    block_type="text",
                    text=text,
                    max_font_size=max_size,
                    bold=bold,
                    source_order=index,
                    line_count=line_count,
                )
            )
        elif raw.get("type") == 1:
            result.append(
                PdfBlock(
                    page_number=page.number + 1,
                    bbox=bbox,
                    block_type="image",
                    image_bytes=raw.get("image"),
                    image_extension=raw.get("ext"),
                    source_order=index,
                )
            )
    return result, font_sizes


def overlap_ratio(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> float:
    """返回 first 面积被 second 覆盖的比例。"""
    rectangle = fitz.Rect(first)
    intersection = rectangle & fitz.Rect(second)
    if rectangle.is_empty or intersection.is_empty:
        return 0.0
    return intersection.get_area() / rectangle.get_area()


def normalized_margin_text(text: str) -> str:
    """归一化页眉页脚中的页码和空白。"""
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip()


def repeated_margin_texts(pages: list[tuple[fitz.Rect, list[PdfBlock]]]) -> set[str]:
    """找出跨页重复的页眉页脚文本。"""
    counts: Counter[str] = Counter()
    for page_rect, blocks in pages:
        seen: set[str] = set()
        for block in blocks:
            if block.block_type != "text":
                continue
            if (
                block.bbox[1] <= page_rect.height * 0.08
                or block.bbox[3] >= page_rect.height * 0.92
            ):
                normalized = normalized_margin_text(block.text)
                if normalized:
                    seen.add(normalized)
        counts.update(seen)
    threshold = max(2, len(pages) // 2)
    return {text for text, count in counts.items() if count >= threshold}


def classify_caption(block: PdfBlock) -> None:
    """原地标注 figure/table caption。"""
    match = CAPTION_PATTERN.match(block.text.strip())
    if not match:
        return
    raw_kind = match.group("kind").lower()
    block.caption_kind = "table" if raw_kind == "table" else "figure"
    prefix = "Table" if block.caption_kind == "table" else "Figure"
    block.caption_label = f"{prefix} {match.group('number')}"
    block.caption_text = match.group("caption").strip()


def heading_level(
    block: PdfBlock, body_font: float, heading_sizes: list[float]
) -> int | None:
    """综合编号、关键词、字号和粗体判断标题层级。"""
    text = block.text.strip()
    normalized = re.sub(r"\s+", " ", text.lower()).rstrip(":")
    if len(text) > 140 or text.count(".") > 4 or len(text.split()) > 18:
        return None
    numbered = re.match(r"^(\d+(?:\.\d+){0,4})\.?\s+", text)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 5)
    appendix = APPENDIX_SUBHEADING.match(text)
    if appendix:
        return min(appendix.group(1).count(".") + 1, 5)
    semantic = normalized in KEYWORD_HEADINGS
    if semantic or re.match(r"^[A-Z]\.?\s+[A-Z]", text):
        return 1
    large = block.max_font_size >= body_font * 1.16
    emphasized = block.bold and block.max_font_size >= body_font * 1.03
    if (
        not semantic
        and not large
        and not emphasized
        and not NUMBERED_HEADING.match(text)
    ):
        return None
    distinct = sorted({round(value, 1) for value in heading_sizes}, reverse=True)
    try:
        return min(distinct.index(round(block.max_font_size, 1)) + 1, 5)
    except ValueError:
        # 字号未进入标题字号集合 → 使用最保守的一级标题
        return 1


def normalized_heading_title(text: str) -> str:
    """移除常见章节编号，返回可用于语义分类的标题。"""
    normalized = re.sub(r"\s+", " ", text.strip().lower()).rstrip(":")
    return re.sub(
        r"^(?:\d+(?:\.\d+){0,4}|[a-z](?:\.\d+){0,4})\.?\s+",
        "",
        normalized,
    )


def visible_title(
    blocks: list[PdfBlock], page_height: float, body_font: float
) -> PdfBlock | None:
    """从首页上部选择显著标题，避免采用 arXiv 页眉式 metadata。"""
    candidates = [
        block
        for block in blocks
        if block.block_type == "text"
        and block.bbox[1] < page_height * 0.28
        and 8 <= len(block.text.strip()) <= 240
        and "arxiv:" not in block.text.lower()
        and "@" not in block.text
    ]
    if not candidates:
        return None
    selected = max(
        candidates, key=lambda block: (block.max_font_size, block.width, -block.bbox[1])
    )
    return selected if selected.max_font_size >= body_font * 1.3 else None


def front_matter_boilerplate(text: str) -> bool:
    """识别 arXiv 页眉、日期等不应进入论文正文的首页噪声。"""
    normalized = text.strip().lower()
    return normalized.startswith(("arxiv:", "preprint"))


def looks_like_author_line(text: str) -> bool:
    """识别标题与摘要之间的作者行，避免污染 heading path。"""
    has_author_markers = bool(re.search(r"[*†‡\d]", text))
    normalized = re.sub(r"[*†‡\d]", "", text).strip()
    if not normalized or len(normalized) > 220 or "@" in normalized:
        return False
    lowered = normalized.lower()
    if any(
        word in lowered
        for word in (
            "university",
            "institute",
            "laboratory",
            "correspondence",
            "abstract",
        )
    ):
        return False
    if normalized.endswith((".", ":")):
        return False
    capitalized = re.findall(r"\b[A-Z][A-Za-z-]+\b", normalized)
    return len(capitalized) >= 4 or (has_author_markers and len(capitalized) >= 2)


def extract_tables(
    page: fitz.Page, diagnostics: list[ProcessingDiagnostic]
) -> list[PdfBlock]:
    """调用 PyMuPDF table finder，并保留表格 bbox 与 Markdown。"""
    blocks: list[PdfBlock] = []
    try:
        finder = page.find_tables()
    except Exception as error:  # noqa: BLE001 - PyMuPDF backend boundary
        diagnostics.append(
            ProcessingDiagnostic(
                level="warning",
                code="pdf_table_detection_failed",
                message=f"PyMuPDF table detection failed on page {page.number + 1}: {error}",
                locator=SourceLocator(source_kind="pdf", page_number=page.number + 1),
            )
        )
        return blocks
    for index, table in enumerate(finder.tables):
        markdown = markdown_table(table.extract())
        if not markdown:
            continue
        blocks.append(
            PdfBlock(
                page_number=page.number + 1,
                bbox=round_bbox(table.bbox),
                block_type="table",
                text=f"Table {index + 1}",
                table_markdown=markdown,
                source_order=10000 + index,
            )
        )
    return blocks


def closest_caption(
    visual: PdfBlock, captions: list[PdfBlock], kind: str
) -> PdfBlock | None:
    """选择视觉区域下方或轻微上方最近且横向相交的同类 caption。"""
    candidates: list[tuple[float, PdfBlock]] = []
    for caption in captions:
        if caption.caption_kind != kind:
            continue
        horizontal = max(
            0.0,
            min(visual.bbox[2], caption.bbox[2]) - max(visual.bbox[0], caption.bbox[0]),
        )
        if horizontal <= 0:
            continue
        if caption.bbox[1] >= visual.bbox[3]:
            distance = caption.bbox[1] - visual.bbox[3]
        else:
            distance = visual.bbox[1] - caption.bbox[3] + 30
        if -10 <= distance <= 180:
            candidates.append((distance, caption))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def horizontal_overlap(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> float:
    """返回两个区域的横向交叠长度。"""
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0]))


def inferred_visual_region(
    page: fitz.Page,
    caption: PdfBlock,
    blocks: list[PdfBlock],
    captions: list[PdfBlock] | None = None,
) -> tuple[float, float, float, float]:
    """从 caption 上方的绘图簇、嵌入图片或表格短文本推断视觉主体。"""
    previous_caption_y = 0.0
    if captions:
        previous_caption_y = max(
            (
                item.bbox[3]
                for item in captions
                if item is not caption
                and item.caption_kind == caption.caption_kind
                and item.bbox[3] <= caption.bbox[1]
                and horizontal_overlap(item.bbox, caption.bbox) > 0
            ),
            default=0.0,
        )
    candidates: list[tuple[float, fitz.Rect]] = []
    if hasattr(page, "cluster_drawings"):
        for rectangle in page.cluster_drawings():
            gap = caption.bbox[1] - rectangle.y1
            overlap = horizontal_overlap(tuple(rectangle), caption.bbox)
            if previous_caption_y <= rectangle.y0 and -5 <= gap <= 260 and overlap > 0:
                candidates.append((gap, rectangle))
    if candidates:
        return round_bbox(min(candidates, key=lambda item: item[0])[1])

    nearby_images = [
        fitz.Rect(block.bbox)
        for block in blocks
        if block.block_type == "image"
        and block.bbox[1] >= previous_caption_y
        and -5 <= caption.bbox[1] - block.bbox[3] <= 260
        and horizontal_overlap(block.bbox, caption.bbox) > 0
    ]
    if nearby_images:
        union = nearby_images[0]
        for rectangle in nearby_images[1:]:
            union |= rectangle
        return round_bbox(union)

    same_column = [
        block
        for block in blocks
        if block.block_type == "text"
        and block is not caption
        and block.bbox[1] >= previous_caption_y
        and 0 <= caption.bbox[1] - block.bbox[3] <= 260
        and horizontal_overlap(block.bbox, caption.bbox) > 0
    ]
    selected: list[PdfBlock] = []
    for block in sorted(same_column, key=lambda item: item.bbox[3], reverse=True):
        word_count = len(block.text.split())
        prose = word_count > 24 and block.text.rstrip().endswith((".", ";"))
        if prose and selected:
            break
        if not prose:
            selected.append(block)
    if selected:
        x0 = min(block.bbox[0] for block in selected)
        y0 = min(block.bbox[1] for block in selected)
        x1 = max(block.bbox[2] for block in selected)
        return round_bbox((x0, y0, x1, caption.bbox[1]))

    wide = caption.width >= page.rect.width * 0.55
    x0 = 50.0 if wide else max(0.0, caption.bbox[0] - 12)
    x1 = page.rect.width - 50.0 if wide else min(page.rect.width, caption.bbox[2] + 12)
    y0 = max(0.0, caption.bbox[1] - min(240.0, page.rect.height * 0.32))
    return round_bbox((x0, y0, x1, caption.bbox[1]))


def text_quality_issues(text: str) -> list[str]:
    """只标记足以证明需要 LLM 修复的明显文本损坏。"""
    issues: list[str] = []
    if "�" in text or UNINDEXABLE_PDF_CHARACTER.search(text):
        issues.append("pdf_invalid_characters")
    letters = sum(char.isalpha() for char in text)
    spaces = text.count(" ")
    if letters > 120 and spaces < letters / 35:
        issues.append("pdf_probable_word_spacing_loss")
    return issues


def looks_like_equation(block: PdfBlock, body_font: float, page_width: float) -> bool:
    """识别独立单行复杂公式，避免把正文行内公式交给 VLM。"""
    text = block.text.strip()
    if (
        not text
        or block.height > 36
        or len(text) > 240
        or block.max_font_size > body_font * 1.2
    ):
        return False
    symbols = ("=", "∑", "∫", "√", "≤", "≥", "≈", "±", "→", "∂", "∇")
    operators = sum(text.count(symbol) for symbol in symbols)
    if operators == 0:
        return False
    if len(text) < 5 or not re.search(r"[_^{}()\[\]×∑∫√≤≥≈±→∂∇α-ωΑ-Ω]", text):
        return False
    first_operator = min(
        (index for index, char in enumerate(text) if char in symbols),
        default=len(text),
    )
    prefix = text[:first_operator]
    prefix_words = re.findall(r"[A-Za-z]{2,}", prefix)
    if len(prefix_words) > 2 or (
        len(prefix_words) > 1 and not re.search(r"[\\{}()\[\]_]", prefix)
    ):
        return False
    center = (block.bbox[0] + block.bbox[2]) / 2
    column_centers = (page_width / 4, page_width / 2, page_width * 3 / 4)
    centered = (
        min(abs(center - candidate) for candidate in column_centers)
        <= page_width * 0.12
    )
    prose_words = len(re.findall(r"\b[A-Za-z]{4,}\b", text))
    return centered and prose_words <= 8 and block.width <= page_width * 0.75


def formula_layout_fragments(
    blocks: list[PdfBlock], body_font: float, page_width: float
) -> set[int]:
    """识别编号展示公式的附属字形块，避免把不可靠的版面顺序当作 prose。"""
    anchors = [
        block
        for block in blocks
        if NUMBERED_EQUATION.search(block.text)
        and (
            re.search(r"[=∑∫√≤≥≈±→∂∇×πθϕγβτˆ∈∉−·]", block.text)
            or re.fullmatch(r"[\s,.;]*\(\d+\)", block.text)
            or re.match(r"^[A-Za-z].*\(\d+\)$", block.text)
        )
    ]
    fragments: set[int] = set()
    math_signal = re.compile(r"[=∑∫√≤≥≈±→∂∇×πθϕγβτˆ∈∉−·]|\b(?:min|max|clip)\b")
    for block in blocks:
        block_center = (block.bbox[0] + block.bbox[2]) / 2
        for anchor in anchors:
            anchor_center = (anchor.bbox[0] + anchor.bbox[2]) / 2
            same_column = (block_center < page_width / 2) == (
                anchor_center < page_width / 2
            )
            nearby = (
                block.bbox[3] >= anchor.bbox[1] - 35
                and block.bbox[1] <= anchor.bbox[3] + 50
            )
            if not same_column or not nearby:
                continue
            complete_standalone = block is anchor and looks_like_equation(
                block, body_font, page_width
            )
            prose_words = len(re.findall(r"\b[A-Za-z]{3,}\b", block.text))
            if not complete_standalone and (
                math_signal.search(block.text)
                and (prose_words <= 5 or len(block.text) <= 45)
                or len(block.text) <= 15
            ):
                fragments.add(id(block))
            break
    return fragments


@dataclass(slots=True)
class PdfParseSession:
    """Mutable state accumulated while parsing one PDF document."""

    elements: list[ParsedElement] = field(default_factory=list)
    visuals: list[ParsedVisual] = field(default_factory=list)
    diagnostics: list[ProcessingDiagnostic] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _PdfContext:
    """Document-scoped values shared by every page."""

    body_font: float
    title_block: PdfBlock | None
    authors: list[str]


@dataclass(slots=True)
class _PageAnalysis:
    """Deterministic analysis consumed for one PDF page."""

    page: fitz.Page
    text_blocks: list[PdfBlock]
    visual_blocks: list[tuple[PdfBlock, PdfBlock | None]]
    used_captions: set[int]
    formula_fragments: set[int]


class PdfPaperParser:
    """PyMuPDF 论文版面解析器。"""

    def __init__(self, pdf_bytes: bytes) -> None:
        self.pdf_bytes = pdf_bytes
        self.session = PdfParseSession()

    def make_id(self, prefix: str, page: int, value: str) -> str:
        """由页码和内容生成稳定标识。"""
        digest = hashlib.sha256(f"{page}:{value}".encode()).hexdigest()[:12]
        return f"{prefix}-{digest}"

    def locator(self, block: PdfBlock) -> SourceLocator:
        """把 PDF block 转成可持久化定位。"""
        return SourceLocator(
            source_kind="pdf", page_number=block.page_number, bbox=block.bbox
        )

    def add_element(
        self,
        block: PdfBlock,
        kind,
        markdown: str,
        visual_ids: list[str] | None = None,
        labels: list[str] | None = None,
        reference_keys: list[str] | None = None,
    ) -> ParsedElement | None:
        """添加单个 PDF 结构元素。"""
        normalized = markdown.strip()
        if not normalized:
            return None
        element = ParsedElement(
            element_id=self.make_id(
                kind, block.page_number, f"{block.bbox}:{normalized}"
            ),
            kind=kind,
            markdown=normalized,
            heading_path=list(self.session.heading_path),
            locators=[self.locator(block)],
            citation_keys=citation_keys(block.text),
            labels=labels or [],
            visual_ids=visual_ids or [],
            repair_issue_codes=block.repair_issue_codes,
            reference_keys=(
                display_reference_keys(block.text)
                if reference_keys is None
                else reference_keys
            ),
        )
        self.session.elements.append(element)
        return element

    def update_heading(self, level: int, title: str) -> None:
        """更新 PDF 标题层级路径。"""
        self.session.heading_path = self.session.heading_path[: level - 1]
        self.session.heading_path.append(title)

    def add_visual_element(
        self, page: fitz.Page, block: PdfBlock, caption: PdfBlock | None
    ) -> None:
        """持久化前记录 figure/table 区域和文本占位符。"""
        kind = "table" if block.block_type == "table" else "figure"
        label = caption.caption_label if caption else None
        caption_text = caption.caption_text if caption else ""
        visual_id = self.make_id(kind, block.page_number, f"{block.bbox}:{label or ''}")
        preview = render_page_region(page, block.bbox)
        structured = block.table_markdown or None
        markdown_body = block.table_markdown if kind == "table" else ""
        markdown = f"**{label or kind.title()}**: {caption_text}"
        if markdown_body:
            markdown += f"\n\n{markdown_body}"
        markdown += f"\n\n{VISUAL_TOKEN.format(visual_id=visual_id)}"
        element = self.add_element(
            block,
            kind,
            markdown,
            [visual_id],
            [label] if label else [],
            reference_keys=[],
        )
        if element is None:
            return
        media_type = (
            "image/jpeg"
            if block.image_extension in {"jpg", "jpeg"}
            else (
                f"image/{block.image_extension}"
                if block.image_extension
                else "image/png"
            )
        )
        self.session.visuals.append(
            ParsedVisual(
                visual_id=visual_id,
                kind=kind,
                locator=self.locator(block),
                element_id=element.element_id,
                label=label,
                caption=caption_text,
                asset_bytes=block.image_bytes or preview,
                asset_media_type=media_type,
                preview_bytes=preview,
                structured_text=structured,
                surrounding_text=caption.text if caption else "",
            )
        )

    def add_equation_visual_element(self, page: fitz.Page, block: PdfBlock) -> None:
        """把 PDF 独立公式作为视觉任务交给后续 VLM，同时保留提取文本证据。"""
        visual_id = self.make_id(
            "equation", block.page_number, f"{block.bbox}:{block.text}"
        )
        x0, y0, x1, y1 = block.bbox
        region = (
            max(0.0, x0 - 12.0),
            max(0.0, y0 - 12.0),
            min(page.rect.width, x1 + 12.0),
            min(page.rect.height, y1 + 12.0),
        )
        preview = render_page_region(page, region)
        markdown = (
            f"```math\n{block.text}\n```\n\n"
            f"{VISUAL_TOKEN.format(visual_id=visual_id)}"
        )
        element = self.add_element(
            block, "equation", markdown, [visual_id], reference_keys=[]
        )
        if element is None:
            return
        self.session.visuals.append(
            ParsedVisual(
                visual_id=visual_id,
                kind="equation",
                locator=self.locator(block),
                element_id=element.element_id,
                caption=block.text,
                asset_bytes=preview,
                asset_media_type="image/png",
                preview_bytes=preview,
                structured_text=block.text,
                surrounding_text="Interpret the equation faithfully, preserving symbols, operators, and relationships.",
            )
        )

    def add_scanned_page(self, page: fitz.Page, extracted_text: str = "") -> None:
        """把低文本页面交给 VLM OCR，并将解释插回正文。"""
        bbox = round_bbox(page.rect)
        block = PdfBlock(page_number=page.number + 1, bbox=bbox, block_type="page")
        visual_id = self.make_id("page", block.page_number, str(bbox))
        token = VISUAL_TOKEN.format(visual_id=visual_id)
        element = self.add_element(block, "paragraph", token, [visual_id])
        if element is None:
            return
        preview = render_page_region(page, bbox, dpi=160)
        self.session.visuals.append(
            ParsedVisual(
                visual_id=visual_id,
                kind="page",
                locator=self.locator(block),
                element_id=element.element_id,
                label=f"Page {block.page_number}",
                asset_bytes=preview,
                asset_media_type="image/png",
                preview_bytes=preview,
                structured_text=extracted_text.strip() or None,
                surrounding_text="Recover all readable paper content from this low-text page without summarizing.",
            )
        )
        self.session.diagnostics.append(
            ProcessingDiagnostic(
                level="warning",
                code="pdf_page_requires_visual_ocr",
                message=f"Page {block.page_number} contained too little extractable text and was delegated to visual interpretation.",
                locator=self.locator(block),
            )
        )

    @staticmethod
    def merged_locators(elements: list[ParsedElement]) -> list[SourceLocator]:
        """按出现顺序合并元素定位。"""
        result: list[SourceLocator] = []
        seen: set[tuple[Any, ...]] = set()
        for element in elements:
            for locator in element.locators:
                key = (
                    locator.source_kind,
                    locator.file,
                    locator.line_start,
                    locator.line_end,
                    locator.page_number,
                    locator.bbox,
                )
                if key not in seen:
                    seen.add(key)
                    result.append(locator)
        return result

    def merge_elements(
        self, elements: list[ParsedElement], kind: str, markdown: str
    ) -> ParsedElement:
        """合并同类 PDF 元素，并重新计算内容相关的稳定 ID。"""
        locators = self.merged_locators(elements)
        page = locators[0].page_number or 0
        identity = f"{[(item.page_number, item.bbox) for item in locators]}:{markdown}"
        return ParsedElement(
            element_id=self.make_id(kind, page, identity),
            kind=kind,
            markdown=markdown,
            heading_path=list(elements[0].heading_path),
            locators=locators,
            citation_keys=list(
                dict.fromkeys(
                    key for element in elements for key in element.citation_keys
                )
            ),
            labels=list(
                dict.fromkeys(label for element in elements for label in element.labels)
            ),
            visual_ids=list(
                dict.fromkeys(
                    visual for element in elements for visual in element.visual_ids
                )
            ),
            repair_issue_codes=list(
                dict.fromkeys(
                    code for element in elements for code in element.repair_issue_codes
                )
            ),
            reference_keys=list(
                dict.fromkeys(
                    key for element in elements for key in element.reference_keys
                )
            ),
        )

    def reflow_split_paragraphs(self) -> None:
        """合并被分页或浮动体打断的同章节 prose，修复跨 block 断词。"""
        index = 0
        while index < len(self.session.elements):
            current = self.session.elements[index]
            if current.kind not in {"abstract", "paragraph"}:
                index += 1
                continue
            following = index + 1
            while following < len(self.session.elements) and self.session.elements[
                following
            ].kind in {
                "figure",
                "front_matter",
                "table",
            }:
                following += 1
            if following >= len(self.session.elements):
                break
            continuation = self.session.elements[following]
            same_context = (
                continuation.kind == current.kind
                and continuation.heading_path == current.heading_path
            )
            starts_lower = continuation.markdown[:1].islower()
            incomplete = not re.search(r"[.!?][\"'”’\])]*$", current.markdown)
            if not same_context or not starts_lower or not incomplete:
                index += 1
                continue
            if current.markdown.endswith("-"):
                markdown = current.markdown[:-1] + continuation.markdown
            else:
                markdown = current.markdown + " " + continuation.markdown
            self.session.elements[index] = self.merge_elements(
                [current, continuation], current.kind, markdown
            )
            del self.session.elements[following]

    @staticmethod
    def bibliography_entry_complete(text: str) -> bool:
        """判断版面块是否已包含作者年份之后的完整出处。"""
        year = BIBLIOGRAPHY_YEAR.search(text)
        if year is None:
            return False
        suffix = text[year.end() :].strip()
        return len(suffix) >= 20 and bool(re.search(r"[.!?]$", suffix))

    @staticmethod
    def join_bibliography_text(elements: list[ParsedElement]) -> str:
        """连接一个参考文献条目的版面块，并只修复确定的断词连字符。"""
        result = ""
        for element in elements:
            value = element.markdown.strip()
            if not result:
                result = value
            elif result.endswith("-") and value[:1].islower():
                result = result[:-1] + value
            else:
                result += " " + value
        return result

    def merge_bibliography_elements(self) -> None:
        """把 References 中的连续版面块恢复为独立、原子的参考文献条目。"""
        result: list[ParsedElement] = []
        pending: list[ParsedElement] = []

        def flush() -> None:
            """Commit the pending bibliography elements as one merged entry."""
            if not pending:
                return
            markdown = self.join_bibliography_text(pending)
            result.append(self.merge_elements(pending, "bibliography", markdown))
            pending.clear()

        for element in self.session.elements:
            if element.kind != "bibliography":
                flush()
                result.append(element)
                continue
            if not pending and re.fullmatch(r"\d+\.?", element.markdown.strip()):
                continue
            pending.append(element)
            if self.bibliography_entry_complete(self.join_bibliography_text(pending)):
                flush()
        flush()
        self.session.elements = result

    def infer_visual_semantic_headings(self) -> None:
        """用最近且唯一的正文显示引用推断视觉语义路径。"""
        contexts: dict[str, list[tuple[int, list[str]]]] = {}
        for index, element in enumerate(self.session.elements):
            if element.kind in {"figure", "table"}:
                continue
            for key in element.reference_keys:
                if element.heading_path:
                    contexts.setdefault(key, []).append(
                        (index, list(element.heading_path))
                    )
        for index, element in enumerate(self.session.elements):
            if element.kind not in {"figure", "table"} or not element.labels:
                continue
            candidates = [
                (abs(reference_index - index), reference_index, path)
                for label in element.labels
                for reference_index, path in contexts.get(label, [])
            ]
            if not candidates:
                continue
            distance = min(item[0] for item in candidates)
            nearest = [item for item in candidates if item[0] == distance]
            paths = {tuple(item[2]) for item in nearest}
            if len(paths) != 1:
                self.session.diagnostics.append(
                    ProcessingDiagnostic(
                        level="warning",
                        code="pdf_visual_semantic_heading_ambiguous",
                        message=f"Could not infer one semantic heading for {element.element_id}.",
                        locator=element.locators[0] if element.locators else None,
                    )
                )
                continue
            semantic_path = list(next(iter(paths)))
            if semantic_path == element.heading_path:
                continue
            element.semantic_heading_path = semantic_path
            self.session.diagnostics.append(
                ProcessingDiagnostic(
                    level="info",
                    code="pdf_visual_semantic_heading_inferred",
                    message=(
                        f"Mapped {element.element_id} from {' / '.join(element.heading_path)} "
                        f"to {' / '.join(semantic_path)} using its nearest display reference."
                    ),
                    locator=element.locators[0] if element.locators else None,
                )
            )

    def classified_text_blocks(
        self,
        blocks: list[PdfBlock],
        margins: set[str],
        body_font: float,
        heading_sizes: list[float],
    ) -> list[PdfBlock]:
        """移除重复页边并标注 caption、heading 和低置信文本。"""
        text_blocks = [
            block
            for block in blocks
            if block.block_type == "text"
            and normalized_margin_text(block.text) not in margins
        ]
        for block in text_blocks:
            classify_caption(block)
            block.heading_level = heading_level(block, body_font, heading_sizes)
            block.repair_issue_codes = text_quality_issues(block.text)
        return text_blocks

    def page_visual_blocks(
        self,
        page: fitz.Page,
        page_rect: fitz.Rect,
        blocks: list[PdfBlock],
        text_blocks: list[PdfBlock],
    ) -> tuple[list[tuple[PdfBlock, PdfBlock | None]], set[int]]:
        """发现或推断本页 figure/table 区域，并返回已消费的 caption。"""
        captions = [block for block in text_blocks if block.caption_kind]
        inferred_figures: list[tuple[PdfBlock, PdfBlock]] = []
        for caption in [item for item in captions if item.caption_kind == "figure"]:
            inferred_figures.append(
                (
                    PdfBlock(
                        page_number=caption.page_number,
                        bbox=inferred_visual_region(page, caption, blocks, captions),
                        block_type="image",
                        source_order=caption.source_order,
                    ),
                    caption,
                )
            )
        tables = [
            table
            for table in extract_tables(page, self.session.diagnostics)
            if not any(
                overlap_ratio(table.bbox, figure.bbox) > 0.5
                for figure, _ in inferred_figures
            )
        ]
        images = [
            block
            for block in blocks
            if block.block_type == "image"
            and fitz.Rect(block.bbox).get_area()
            >= page_rect.get_area() * MIN_VISUAL_AREA_RATIO
            and not any(overlap_ratio(block.bbox, table.bbox) > 0.5 for table in tables)
            and not any(
                overlap_ratio(block.bbox, figure.bbox) > 0.7
                for figure, _ in inferred_figures
            )
        ]
        visual_blocks: list[tuple[PdfBlock, PdfBlock | None]] = list(inferred_figures)
        used_captions = {id(caption) for _, caption in inferred_figures}
        for table in tables:
            caption = closest_caption(table, captions, "table")
            if caption:
                table.text = caption.text
                used_captions.add(id(caption))
            visual_blocks.append((table, caption))
        for image in images:
            caption = closest_caption(image, captions, "figure")
            if caption:
                used_captions.add(id(caption))
            visual_blocks.append((image, caption))
        for caption in captions:
            if caption.caption_kind != "table" or id(caption) in used_captions:
                continue
            visual_blocks.append(
                (
                    PdfBlock(
                        page_number=caption.page_number,
                        bbox=inferred_visual_region(page, caption, blocks, captions),
                        block_type="table",
                        text=caption.text,
                        source_order=caption.source_order,
                    ),
                    caption,
                )
            )
            used_captions.add(id(caption))
        for visual, caption in visual_blocks:
            if caption is None or visual.table_markdown:
                continue
            self.session.diagnostics.append(
                ProcessingDiagnostic(
                    level="info",
                    code="pdf_visual_region_inferred",
                    message=f"Inferred a {caption.caption_kind} region near {caption.caption_label}.",
                    locator=self.locator(visual),
                )
            )
        return visual_blocks, used_captions

    def _consume_page(self, page_data: _PageAnalysis, context: _PdfContext) -> None:
        """按阅读顺序把一页分析结果写入统一元素与视觉列表。"""
        page = page_data.page
        page_rect = page.rect
        text_blocks = page_data.text_blocks
        visual_blocks = page_data.visual_blocks
        used_captions = page_data.used_captions
        formula_fragments = page_data.formula_fragments
        title_block = context.title_block
        authors = context.authors
        body_font = context.body_font
        visual_by_id = {id(block): caption for block, caption in visual_blocks}
        content_blocks = [
            block
            for block in text_blocks
            if id(block) not in used_captions
            and id(block) not in formula_fragments
            and (
                block.heading_level is not None
                or not any(
                    overlap_ratio(block.bbox, visual.bbox) > 0.55
                    for visual, _ in visual_blocks
                )
            )
        ]
        page_text = " ".join(block.text for block in text_blocks)
        if len(re.sub(r"\s+", "", page_text)) < 60:
            self.add_scanned_page(page, page_text)
            return
        if formula_fragments:
            first = next(
                block for block in text_blocks if id(block) in formula_fragments
            )
            self.session.diagnostics.append(
                ProcessingDiagnostic(
                    level="warning",
                    code="pdf_formula_layout_fragment_omitted",
                    message=(
                        f"Omitted {len(formula_fragments)} unreliable glyph fragments around "
                        f"numbered equations on page {page.number + 1}."
                    ),
                    locator=self.locator(first),
                )
            )
        ordered = reading_order(
            content_blocks + [pair[0] for pair in visual_blocks], page_rect.width
        )
        abstract_y = min(
            (
                item.bbox[1]
                for item in text_blocks
                if item.text.strip().lower() == "abstract"
            ),
            default=0.0,
        )
        for block in ordered:
            if id(block) in visual_by_id:
                self.add_visual_element(page, block, visual_by_id[id(block)])
            elif block.caption_kind:
                self.add_element(block, "paragraph", block.text)
            elif (
                page.number == 0
                and title_block is not None
                and block.text == title_block.text
            ):
                self.add_element(block, "front_matter", f"# {block.text.strip()}")
            elif page.number == 0 and abstract_y and block.bbox[1] < abstract_y:
                if front_matter_boilerplate(block.text):
                    continue
                if (
                    looks_like_author_line(block.text)
                    and block.text.strip() not in authors
                ):
                    for author in split_pdf_author_line(block.text):
                        if author not in authors:
                            authors.append(author)
                if block.bbox[1] > page_rect.height * 0.25:
                    continue
                self.add_element(block, "front_matter", block.text)
            elif block.heading_level:
                title = block.text.strip()
                self.update_heading(block.heading_level, title)
                self.add_element(
                    block, "heading", f"{'#' * (block.heading_level + 1)} {title}"
                )
            elif looks_like_equation(block, body_font, page_rect.width):
                self.add_equation_visual_element(page, block)
                self.session.diagnostics.append(
                    ProcessingDiagnostic(
                        level="info",
                        code="pdf_equation_text_only",
                        message="Preserved extracted PDF equation text and attached a visual interpretation task because TeX notation is unavailable.",
                        locator=self.locator(block),
                    )
                )
            elif not (page.number == 0 and front_matter_boilerplate(block.text)):
                if (
                    re.fullmatch(r"\d+\s*https?://\S+/?", block.text)
                    and block.bbox[1] > page_rect.height * 0.8
                ):
                    continue
                section = (
                    normalized_heading_title(self.session.heading_path[-1])
                    if self.session.heading_path
                    else ""
                )
                kind = (
                    "abstract"
                    if section == "abstract"
                    else (
                        "bibliography"
                        if section in {"references", "bibliography"}
                        else (
                            "front_matter"
                            if re.match(
                                r"^[*∗†‡].*(?:contribution|author)",
                                block.text,
                                re.IGNORECASE,
                            )
                            else "paragraph"
                        )
                    )
                )
                self.add_element(block, kind, block.text)

    def parse(self) -> ParsedPaper:
        """解析 PDF 全文，保留页码、bbox、表格和视觉解释入口。"""
        try:
            document = fitz.open(stream=self.pdf_bytes, filetype="pdf")
        except (RuntimeError, ValueError) as error:
            # PyMuPDF 无法打开损坏或非 PDF 输入 → 拒绝来源
            raise ValueError(f"Invalid PDF source: {error}") from error
        with document:
            page_data: list[tuple[fitz.Rect, list[PdfBlock]]] = []
            all_sizes: list[float] = []
            for page in document:
                blocks, sizes = extract_page_blocks(page)
                page_data.append((page.rect, blocks))
                all_sizes.extend(sizes)
            body_font = statistics.median(all_sizes) if all_sizes else 10.0
            heading_sizes = [
                block.max_font_size
                for _, blocks in page_data
                for block in blocks
                if block.max_font_size >= body_font * 1.03
            ]
            margins = repeated_margin_texts(page_data)
            metadata = document.metadata or {}
            metadata_title = str(metadata.get("title") or "").strip()
            title_block = (
                visible_title(page_data[0][1], page_data[0][0].height, body_font)
                if page_data
                else None
            )
            recovered_title = (
                title_block.text.strip() if title_block else metadata_title
            )
            authors = split_metadata_authors(str(metadata.get("author") or ""))
            context = _PdfContext(body_font, title_block, authors)

            for page_index, page in enumerate(document):
                page_rect, blocks = page_data[page_index]
                text_blocks = self.classified_text_blocks(
                    blocks, margins, body_font, heading_sizes
                )
                formula_fragments = formula_layout_fragments(
                    text_blocks, body_font, page_rect.width
                )
                visual_blocks, used_captions = self.page_visual_blocks(
                    page, page_rect, blocks, text_blocks
                )
                self._consume_page(
                    _PageAnalysis(
                        page,
                        text_blocks,
                        visual_blocks,
                        used_captions,
                        formula_fragments,
                    ),
                    context,
                )

            self.reflow_split_paragraphs()
            self.merge_bibliography_elements()
            self.infer_visual_semantic_headings()
            abstract = extract_abstract(self.session.elements)
            bibliography = extract_bibliography(self.session.elements)
            if not self.session.elements:
                raise ValueError(
                    "PDF contains no extractable or visually recoverable content."
                )
            return ParsedPaper(
                source_kind="pdf",
                source_fingerprint=hashlib.sha256(self.pdf_bytes).hexdigest(),
                converter=f"pymupdf-{fitz.VersionBind}",
                title=recovered_title,
                authors=authors,
                abstract=abstract,
                elements=self.session.elements,
                visuals=self.session.visuals,
                diagnostics=self.session.diagnostics,
                bibliography=bibliography,
                source_labels=[
                    visual.label for visual in self.session.visuals if visual.label
                ],
                source_reference_keys=list(
                    dict.fromkeys(
                        key
                        for element in self.session.elements
                        for key in element.reference_keys
                    )
                ),
            )


def split_metadata_authors(value: str) -> list[str]:
    """拆分 PDF metadata 中常见的作者分隔形式。"""
    return [
        item.strip()
        for item in re.split(r"\s*(?:;|,|\band\b)\s*", value)
        if item.strip()
    ]


def split_pdf_author_line(value: str) -> list[str]:
    """按 PDF 作者脚注标记拆分一行中的独立姓名。"""
    parts = re.split(
        r"(?<=[A-Za-z])(?:[∗*†‡]?\d+)(?=\s+[A-Z])",
        value.strip(),
    )
    return [
        re.sub(r"[∗*†‡]?\d+$", "", part).strip()
        for part in parts
        if re.sub(r"[∗*†‡]?\d+$", "", part).strip()
    ]


def extract_abstract(elements: list[ParsedElement]) -> str:
    """从 Abstract 标题后收集正文，直到下一个标题。"""
    collecting = False
    paragraphs: list[str] = []
    for element in elements:
        if element.kind == "heading":
            title = normalized_heading_title(element.markdown.lstrip("# "))
            if collecting:
                break
            collecting = title == "abstract"
        elif collecting and element.kind in {"abstract", "paragraph"}:
            paragraphs.append(element.markdown)
    return "\n\n".join(paragraphs)


def extract_bibliography(elements: list[ParsedElement]) -> str:
    """从 References/Bibliography 标题后收集条目。"""
    collecting = False
    values: list[str] = []
    for element in elements:
        if element.kind == "heading":
            title = normalized_heading_title(element.markdown.lstrip("# "))
            if collecting and title not in {"references", "bibliography"}:
                break
            collecting = title in {"references", "bibliography"}
        elif collecting:
            values.append(element.markdown)
    return "\n\n".join(values)


def parse_pdf_paper(pdf_bytes: bytes) -> ParsedPaper:
    """公开入口：使用 PyMuPDF 构建统一 ``ParsedPaper``。"""
    return PdfPaperParser(pdf_bytes).parse()

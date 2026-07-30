"""按论文结构构建有溯源、不过度切碎的 RAG chunk。"""

import hashlib
import math
from dataclasses import dataclass
from typing import cast

from athena.research.paper_markdown.document import ParsedElement
from athena.research.paper_markdown.schemas import (
    ChunkingConfig,
    ElementKind,
    SourceLocator,
)

OVERLAP_KINDS = {"abstract", "list", "paragraph"}
ATOMIC_ELEMENT_KINDS = {"bibliography", "equation", "figure", "table"}


@dataclass(slots=True)
class DraftChunk:
    """尚未写入 ArtifactStore 的 chunk。"""

    chunk_id: str
    kind: ElementKind
    content_text: str
    retrieval_text: str
    heading_path: list[str]
    semantic_heading_path: list[str]
    char_start: int
    char_end: int
    token_estimate: int
    locators: list[SourceLocator]
    citation_keys: list[str]
    reference_keys: list[str]
    labels: list[str]
    visual_ids: list[str]


def full_markdown(
    elements: list[ParsedElement],
) -> tuple[str, dict[str, tuple[int, int]]]:
    """连接全部结构元素，并返回元素在全文中的字符区间。"""
    parts: list[str] = []
    spans: dict[str, tuple[int, int]] = {}
    offset = 0
    for element in elements:
        if parts:
            parts.append("\n\n")
            offset += 2
        start = offset
        parts.append(element.markdown)
        offset += len(element.markdown)
        spans[element.element_id] = (start, offset)
    return "".join(parts), spans


def unique_locators(elements: list[ParsedElement]) -> list[SourceLocator]:
    """按出现顺序去重多个元素的 TeX/PDF 定位。"""
    result: list[SourceLocator] = []
    seen: set[tuple] = set()
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


def dominant_kind(elements: list[ParsedElement]) -> ElementKind:
    """优先用表格、图、公式等高信息类型标记 chunk，否则使用正文类型。"""
    for preferred in (
        "table",
        "figure",
        "equation",
        "code",
        "bibliography",
        "abstract",
    ):
        if any(element.kind == preferred for element in elements):
            return cast(ElementKind, preferred)
    return (
        "heading"
        if all(element.kind == "heading" for element in elements)
        else "paragraph"
    )


def draft_chunk(
    elements: list[ParsedElement], spans: dict[str, tuple[int, int]]
) -> DraftChunk:
    """把一组完整元素投影为单个检索单元。"""
    heading_path = next(
        (
            element.heading_path
            for element in reversed(elements)
            if element.heading_path
        ),
        [],
    )
    semantic_heading_path = next(
        (
            element.semantic_heading_path
            for element in reversed(elements)
            if element.semantic_heading_path
        ),
        heading_path,
    )
    content_text = "\n\n".join(element.markdown for element in elements)
    if heading_path and not any(element.kind == "heading" for element in elements):
        content_text = f"> Section: {' / '.join(heading_path)}\n\n{content_text}"
    retrieval_text = content_text
    if semantic_heading_path != heading_path:
        # 浮动体可能与源码中紧邻但语义错误的标题成组；检索副本只保留推断后的章节语境。
        content = list(elements)
        while content and content[0].kind == "heading":
            content.pop(0)
        retrieval_body = "\n\n".join(element.markdown for element in content)
        section = f"> Section: {' / '.join(semantic_heading_path)}"
        retrieval_text = section + (f"\n\n{retrieval_body}" if retrieval_body else "")
    start = min(spans[element.element_id][0] for element in elements)
    end = max(spans[element.element_id][1] for element in elements)
    identity = f"{start}:{end}:{content_text}".encode("utf-8")
    return DraftChunk(
        chunk_id="chunk-" + hashlib.sha256(identity).hexdigest()[:16],
        kind=dominant_kind(elements),
        content_text=content_text,
        retrieval_text=retrieval_text,
        heading_path=list(heading_path),
        semantic_heading_path=list(semantic_heading_path),
        char_start=start,
        char_end=end,
        token_estimate=math.ceil(len(retrieval_text) / 4),
        locators=unique_locators(elements),
        citation_keys=list(
            dict.fromkeys(key for element in elements for key in element.citation_keys)
        ),
        reference_keys=list(
            dict.fromkeys(key for element in elements for key in element.reference_keys)
        ),
        labels=list(
            dict.fromkeys(label for element in elements for label in element.labels)
        ),
        visual_ids=list(
            dict.fromkeys(
                visual for element in elements for visual in element.visual_ids
            )
        ),
    )


def build_chunks(
    elements: list[ParsedElement], config: ChunkingConfig
) -> tuple[str, list[DraftChunk]]:
    """按章节边界和目标字符数分组，overlap 只复用完整元素。"""
    markdown, spans = full_markdown(elements)
    groups: list[list[ParsedElement]] = []
    current: list[ParsedElement] = []
    current_chars = 0
    current_section: tuple[str, ...] = ()

    def flush(keep_overlap: bool) -> None:
        nonlocal current, current_chars, current_section
        if not current:
            return
        groups.append(current)
        overlap = (
            current[-config.overlap_elements :]
            if keep_overlap and config.overlap_elements
            else []
        )
        current = [element for element in overlap if element.kind in OVERLAP_KINDS]
        current_chars = sum(len(element.markdown) + 2 for element in current)
        current_section = tuple(current[-1].heading_path) if current else ()

    for element in elements:
        is_atomic = element.kind in ATOMIC_ELEMENT_KINDS
        if is_atomic and current and any(item.kind != "heading" for item in current):
            flush(keep_overlap=False)
        section = tuple(element.heading_path)
        changes_section = bool(current) and section != current_section
        starts_section = changes_section and (
            element.kind != "heading" or any(item.kind != "heading" for item in current)
        )
        if starts_section:
            flush(keep_overlap=False)
        exceeds_target = (
            current and current_chars + len(element.markdown) + 2 > config.target_chars
        )
        if exceeds_target:
            flush(keep_overlap=True)
        current.append(element)
        current_chars += len(element.markdown) + 2
        current_section = section or current_section
        if is_atomic:
            flush(keep_overlap=False)
    flush(keep_overlap=False)
    return markdown, [draft_chunk(group, spans) for group in groups]

"""可确定执行的论文 RAG 摄取质量门禁。"""

import re
from collections import Counter

from athena.research.paper_markdown.chunking import DraftChunk
from athena.research.paper_markdown.document import ParsedPaper
from athena.research.paper_markdown.schemas import (
    ChunkingConfig,
    ProcessingDiagnostic,
    SourceLocator,
)

UNINDEXABLE_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|[\ue000-\uf8ff]")
UNPARSED_APPENDIX_HEADING = re.compile(r"^[A-Z]\.\d+(?:\.\d+){0,3}\.?\s+[A-Z]")


def _add_warning(
    paper: ParsedPaper,
    code: str,
    message: str,
    locator: SourceLocator | None = None,
) -> None:
    """追加稳定、可持久化的质量 warning。"""
    paper.diagnostics.append(
        ProcessingDiagnostic(
            level="warning", code=code, message=message, locator=locator
        )
    )


def _validate_reference_graph(paper: ParsedPaper) -> None:
    """检查活动源码 label、引用目标和元素引用边是否闭合。"""
    id_counts = Counter(element.element_id for element in paper.elements)
    duplicate_ids = sorted(
        identifier for identifier, count in id_counts.items() if count > 1
    )
    if duplicate_ids:
        _add_warning(
            paper,
            "rag_element_id_duplicate",
            f"Found {len(duplicate_ids)} duplicate element IDs.",
        )

    persisted_labels = {
        label for element in paper.elements for label in element.labels if label
    }
    source_labels = set(paper.source_labels)
    if source_labels != persisted_labels:
        _add_warning(
            paper,
            "rag_label_graph_incomplete",
            "Persisted element labels differ from active source labels; "
            f"missing={sorted(source_labels - persisted_labels)}, "
            f"unexpected={sorted(persisted_labels - source_labels)}.",
        )

    unresolved = sorted(set(paper.source_reference_keys) - persisted_labels)
    if unresolved:
        _add_warning(
            paper,
            "rag_cross_reference_unresolved",
            f"Cross-reference targets are not persisted: {unresolved}.",
        )

    persisted_references = {
        key for element in paper.elements for key in element.reference_keys if key
    }
    source_references = set(paper.source_reference_keys)
    if source_references != persisted_references:
        _add_warning(
            paper,
            "rag_reference_edges_incomplete",
            "Persisted element reference keys differ from active source references; "
            f"missing={sorted(source_references - persisted_references)}, "
            f"unexpected={sorted(persisted_references - source_references)}.",
        )


def _validate_elements(paper: ParsedPaper) -> None:
    """检查标题路径和展示公式是否适合 Markdown 检索。"""
    for element in paper.elements:
        locator = element.locators[0] if element.locators else None
        if UNINDEXABLE_CHARACTER.search(element.markdown):
            _add_warning(
                paper,
                "rag_control_character",
                f"Element {element.element_id} contains an unindexable control or private-use character.",
                locator,
            )
        if element.kind != "heading" and UNPARSED_APPENDIX_HEADING.match(
            element.markdown.strip()
        ):
            _add_warning(
                paper,
                "rag_appendix_heading_unparsed",
                f"Element {element.element_id} looks like an appendix heading but is not structured as one.",
                locator,
            )
        if element.kind == "bibliography" and (
            len(element.markdown.strip()) < 40
            or re.fullmatch(r"\d+\.?", element.markdown.strip())
            or re.fullmatch(
                r"arXiv:\s*\d{4}\.\d{4,5}\.?", element.markdown.strip(), re.IGNORECASE
            )
        ):
            _add_warning(
                paper,
                "rag_bibliography_fragment",
                f"Bibliography element {element.element_id} is too short to be an independent citation.",
                locator,
            )
        if "" in element.heading_path:
            _add_warning(
                paper,
                "rag_heading_path_empty",
                f"Element {element.element_id} contains an empty heading path component.",
                locator,
            )
        if element.kind == "heading" and not element.markdown.lstrip("#").strip():
            _add_warning(
                paper,
                "rag_heading_empty",
                f"Element {element.element_id} contains an empty Markdown heading.",
                locator,
            )
        if element.kind != "equation":
            continue
        has_alignment = re.search(
            r"\\begin\{(?:aligned|gathered|cases|array|\w*matrix)\}",
            element.markdown,
        )
        invalid_math = (
            "\\label" in element.markdown
            or "\\nonumber" in element.markdown
            or "\\notag" in element.markdown
            or ("&" in element.markdown and has_alignment is None)
        )
        if invalid_math:
            _add_warning(
                paper,
                "rag_math_markdown_invalid",
                f"Equation {element.element_id} contains unsupported display alignment markup.",
                locator,
            )


def _validate_chunks(
    paper: ParsedPaper,
    chunks: list[DraftChunk],
    markdown: str,
    config: ChunkingConfig,
) -> None:
    """检查 chunk 证据区间、大小、视觉粒度和检索章节。"""
    for chunk in chunks:
        locator = chunk.locators[0] if chunk.locators else None
        chunk_body = chunk.content_text
        if chunk_body.startswith("> Section: "):
            _, separator, remainder = chunk_body.partition("\n\n")
            chunk_body = remainder if separator else chunk_body
        if markdown[chunk.char_start : chunk.char_end] != chunk_body:
            _add_warning(
                paper,
                "rag_chunk_span_mismatch",
                f"Chunk {chunk.chunk_id} cannot be replayed from its Markdown character range.",
                locator,
            )
        if chunk.kind == "heading" and len(chunk.content_text) < 100:
            _add_warning(
                paper,
                "rag_chunk_heading_only",
                f"Chunk {chunk.chunk_id} contains only {len(chunk.content_text)} characters of heading text.",
                locator,
            )
        if len(chunk.content_text) > config.target_chars:
            _add_warning(
                paper,
                "rag_chunk_oversized",
                f"Chunk {chunk.chunk_id} has {len(chunk.content_text)} characters, exceeding the "
                f"{config.target_chars}-character target without splitting a source element.",
                locator,
            )
        if len(chunk.visual_ids) > 1:
            _add_warning(
                paper,
                "rag_chunk_multiple_visuals",
                f"Chunk {chunk.chunk_id} contains {len(chunk.visual_ids)} visual elements.",
                locator,
            )
        _validate_retrieval_heading(paper, chunk, locator)


def _validate_retrieval_heading(
    paper: ParsedPaper,
    chunk: DraftChunk,
    locator: SourceLocator | None,
) -> None:
    """保证检索副本只携带 semantic heading 上下文。"""
    if chunk.semantic_heading_path == chunk.heading_path:
        return
    expected = f"> Section: {' / '.join(chunk.semantic_heading_path)}"
    lines = [line.strip() for line in chunk.retrieval_text.splitlines() if line.strip()]
    section_lines = [line for line in lines if line.startswith("> Section: ")]
    source_headings = set(chunk.heading_path)
    stale_headings = [
        line
        for line in lines
        if (match := re.fullmatch(r"#{1,6}\s+(.+)", line))
        and match.group(1).strip() in source_headings
    ]
    if (
        lines
        and lines[0] == expected
        and section_lines == [expected]
        and not stale_headings
    ):
        return
    _add_warning(
        paper,
        "rag_retrieval_heading_mismatch",
        f"Chunk {chunk.chunk_id} retrieval text does not exclusively use its semantic "
        f"section context: {' / '.join(chunk.semantic_heading_path)}.",
        locator,
    )


def _validate_tables(paper: ParsedPaper) -> None:
    """检查确定性表格是否为表头非空的矩形 Markdown。"""
    for visual in paper.visuals:
        if visual.kind != "table" or not visual.structured_text:
            continue
        lines = [line for line in visual.structured_text.splitlines() if line.strip()]
        table_lines = [
            re.split(r"(?<!\\)\|", line.strip()[1:-1])
            for line in lines
            if line.strip().startswith("|") and line.strip().endswith("|")
        ]
        valid = (
            len(lines) >= 2
            and len(table_lines) == len(lines)
            and len({len(row) for row in table_lines}) == 1
            and all(value.strip() for value in table_lines[0])
        )
        if not valid:
            _add_warning(
                paper,
                "rag_table_markdown_invalid",
                f"Table {visual.visual_id} is not a rectangular Markdown table with non-empty headers.",
                visual.locator,
            )


def validate_rag_quality(
    paper: ParsedPaper,
    chunks: list[DraftChunk],
    markdown: str,
    config: ChunkingConfig,
) -> None:
    """运行全部确定性质量门禁，并把结果写入论文诊断。"""
    _validate_reference_graph(paper)
    _validate_elements(paper)
    _validate_chunks(paper, chunks, markdown, config)
    _validate_tables(paper)

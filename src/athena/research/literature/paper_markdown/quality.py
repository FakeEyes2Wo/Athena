"""可确定执行的论文 RAG 摄取质量门禁。"""

import re
from collections import Counter

from athena.research.literature.contracts import ChunkingConfig, ProcessingDiagnostic
from athena.research.literature.paper_markdown.chunking import DraftChunk
from athena.research.literature.paper_markdown.document import ParsedPaper
from athena.research.literature.paper_markdown.schemas import SourceLocator

UNINDEXABLE_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|[\ue000-\uf8ff]")
UNPARSED_APPENDIX_HEADING = re.compile(r"^[A-Z]\.\d+(?:\.\d+){0,3}\.?\s+[A-Z]")

ALIGNMENT_ENVIRONMENTS = (
    "alignedat",
    "subarray",
    "eqnarray",
    "flalign",
    "gathered",
    "alignat",
    "aligned",
    "gather",
    "dcases",
    "rcases",
    "align",
    "array",
    "cases",
    "split",
)
"""\u516c\u5f0f\u91cc\u5408\u6cd5\u4f7f\u7528 ``&`` \u505a\u5bf9\u9f50\u7684\u73af\u5883\uff0c\u6309\u540d\u5b57\u957f\u5ea6\u964d\u5e8f\u6392\u5217\u3002

\u964d\u5e8f\u662f\u5fc5\u8981\u7684\uff1a\u6b63\u5219\u7684\u5206\u652f\u6309\u987a\u5e8f\u5c1d\u8bd5\uff0c``align`` \u6392\u5728 ``alignat`` \u524d\u9762\u4f1a\u5148\u5339\u914d\u5230
``align`` \u518d\u8981\u6c42\u95ed\u5408\u82b1\u62ec\u53f7\uff0c\u867d\u7136 Python \u7684 ``re`` \u4f1a\u56de\u6eaf\u6551\u56de\u6765\uff0c\u4f46\u987a\u5e8f\u5199\u5bf9\u66f4\u7701\u4e8b
\u4e5f\u66f4\u597d\u8bfb\u3002

\u6e05\u5355\u4e00\u5ea6\u53ea\u6709 ``aligned|gathered|cases|array|*matrix``\uff0c\u6f0f\u6389\u4e86 ``split``\u3002\u5b9e\u6d4b\u4e00\u6279
\u771f\u5b9e\u8bba\u6587\u7684 60 \u4e2a\u516c\u5f0f\u91cc\uff0c8 \u4e2a\u7528 ``split``\u300114 \u4e2a\u7528 ``aligned``\u2014\u2014\u6f0f\u9879\u8ba9\u90a3 8 \u4e2a\u5168\u88ab\u5224
\u6210"\u4e0d\u652f\u6301\u7684\u5bf9\u9f50\u6807\u8bb0"\uff0c\u800c\u5b83\u4eec\u662f\u5b8c\u5168\u5408\u6cd5\u3001KaTeX \u80fd\u6b63\u5e38\u6e32\u67d3\u7684 amsmath \u516c\u5f0f\uff0c\u6b63\u6587\u4e00\u4e2a
\u5b57\u90fd\u6ca1\u4e22\u3002\u4e09\u7bc7\u8bba\u6587\u56e0\u6b64\u88ab\u5224 ``degraded`` \u6321\u5728\u8bed\u6599\u4e4b\u5916\u3002
"""

MATH_ALIGNMENT = re.compile(
    r"\\begin\{(?:"
    + "|".join(ALIGNMENT_ENVIRONMENTS)
    + r")\*?\}|\\begin\{\w*matrix\*?\}"
)

CONTENT_LOSS_CODES = frozenset(
    {
        "pdf_formula_layout_fragment_omitted",
        "pdf_page_requires_visual_ocr",
        "pdf_table_detection_failed",
        "rag_math_markdown_invalid",
        "rag_table_markdown_invalid",
        "structure_refiner_unavailable",
        "tex_bibliography_parse_failed",
        "tex_bibliography_unresolved",
        "tex_document_environment_missing",
        # 这两条的兜底是把 \input 宏原样写回正文，被包含文件的内容并没有进来
        "tex_include_missing",
        "tex_include_non_text",
        # 入口猜错会静默丢掉大半篇论文，宁可按内容缺失处理
        "tex_entrypoint_weak_inference",
        "tex_figure_asset_missing",
        # 渲染不出预览 ⇒ 模型看不到这张图，图里的数据就是没进语料
        "visual_preview_unavailable",
        "visual_interpretation_failed",
        "visual_interpretation_unavailable",
    }
)
BOOKKEEPING_CODES = frozenset(
    {
        "pdf_equation_text_only",
        "pdf_visual_region_inferred",
        "pdf_visual_semantic_heading_ambiguous",
        "pdf_visual_semantic_heading_inferred",
        "rag_appendix_heading_unparsed",
        "rag_bibliography_fragment",
        "rag_chunk_heading_only",
        "rag_chunk_multiple_visuals",
        "rag_chunk_oversized",
        "rag_chunk_span_mismatch",
        "rag_control_character",
        "rag_cross_reference_unresolved",
        "rag_element_id_duplicate",
        "rag_heading_empty",
        "rag_heading_path_empty",
        "rag_label_graph_incomplete",
        "rag_reference_edges_incomplete",
        "rag_retrieval_heading_mismatch",
        "structure_repaired_by_llm",
        # 正文已由包内 PDF 完整恢复，只是记下换了通道；质量由 PDF 通道自己的码决定
        "tex_body_empty_pdf_used",
        "tex_figure_without_asset",
        "tex_float_semantic_heading_ambiguous",
        "tex_float_semantic_heading_inferred",
        "tex_heading_empty",
        # 内容已经找回来了，只是源码里的大小写与磁盘不一致
        "tex_include_case_mismatch",
        # 正文完整，只是部分字符可能解码走样
        "tex_latin1_fallback",
        "tex_table_complex_fallback",
    }
)


def grade_quality(codes: list[str]) -> str:
    """\u6309"\u5185\u5bb9\u662f\u5426\u771f\u7684\u4e22\u4e86"\u7ed9\u8f6c\u6362\u7ed3\u679c\u5b9a\u7ea7\uff0c\u800c\u4e0d\u662f\u6309"\u6709\u6ca1\u6709 warning"\u3002

    \u6b64\u524d\u7684\u89c4\u5219\u662f ``"degraded" if quality_codes else "pass"``\u2014\u2014\u53ea\u8981\u51fa\u73b0\u4efb\u4f55\u4e00\u6761 warning
    \u5c31\u964d\u7ea7\uff0c\u4e8e\u662f"\u6574\u4e2a\u89c6\u89c9\u6a21\u6001\u7f3a\u5931"\u548c"\u67d0\u4e2a chunk \u6bd4\u76ee\u6807\u5927 40%"\u62ff\u5230\u540c\u4e00\u4e2a\u6807\u7b7e\uff0c\u4e0b\u6e38\u770b\u5230
    ``degraded`` \u65e0\u6cd5\u5224\u65ad\u8981\u4e0d\u8981\u91cd\u8dd1\u3002\u5b9e\u6d4b\u4e00\u8f6e\u91cc\u4e09\u7bc7\u5168\u662f ``degraded``\uff0c\u800c\u8bed\u6599\u5b8c\u6574\u3001\u94fe\u63a5\u65e0
    \u60ac\u7a7a\u3001\u68c0\u7d22\u6b63\u5e38\u3002

    \u56e0\u6b64\u5206\u6210\u4e09\u6863\uff1a``degraded`` \u53ea\u7559\u7ed9\u5185\u5bb9\u771f\u7684\u6ca1\u8fdb\u8bed\u6599\u7684\u60c5\u51b5\uff08\u89c6\u89c9\u89e3\u91ca\u7f3a\u5931\u6216\u5931\u8d25\u3001\u8868\u683c/
    \u516c\u5f0f Markdown \u5931\u6548\u3001\u6b63\u6587\u89e3\u6790\u9000\u5316\uff09\uff1b\u5185\u5bb9\u5b8c\u6574\u4f46\u8bb0\u8d26\u4e0d\u7406\u60f3\u7684\u5f52 ``pass_with_notes``\uff1b
    \u6ca1\u6709 warning \u624d\u662f ``pass``\u3002

    \u672a\u767b\u8bb0\u7684\u65b0\u4ee3\u7801\u6309 ``degraded`` \u5904\u7406\u2014\u2014\u6f0f\u62a5\u6bd4\u8bef\u62a5\u5371\u9669\uff0c\u800c
    ``test_paper_quality`` \u91cc\u7684\u8986\u76d6\u6d4b\u8bd5\u4f1a\u5f3a\u5236\u65b0\u4ee3\u7801\u663e\u5f0f\u5f52\u7c7b\u3002

    ``grade_quality(["rag_chunk_oversized"])`` \u8fd4\u56de ``"pass_with_notes"``\u3002
    """
    if not codes:
        return "pass"
    if any(code not in BOOKKEEPING_CODES for code in codes):
        return "degraded"
    return "pass_with_notes"


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
        has_alignment = MATH_ALIGNMENT.search(element.markdown)
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

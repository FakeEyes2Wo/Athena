"""Deterministic paper RAG quality-gate tests."""

import unittest

from athena.research.paper_markdown.chunking import build_chunks
from athena.research.paper_markdown.document import ParsedElement, ParsedPaper
from athena.research.paper_markdown.quality import validate_rag_quality
from athena.research.paper_markdown.schemas import ChunkingConfig, SourceLocator

LOCATOR = SourceLocator(source_kind="tex", file="main.tex", line_start=1, line_end=1)


def paper_with(elements: list[ParsedElement], **values) -> ParsedPaper:
    """Create the minimal parsed paper needed by deterministic quality checks."""
    return ParsedPaper(
        source_kind="tex",
        source_fingerprint="f" * 64,
        converter="test",
        title="",
        authors=[],
        abstract="",
        elements=elements,
        visuals=[],
        diagnostics=[],
        **values,
    )


class PaperQualityTest(unittest.TestCase):
    def validate(self, paper: ParsedPaper) -> None:
        markdown, chunks = build_chunks(paper.elements, ChunkingConfig())
        validate_rag_quality(paper, chunks, markdown, ChunkingConfig())

    def test_detects_retrieval_heading_mismatch(self) -> None:
        table = ParsedElement(
            "table",
            "table",
            "| Method | Score |\n| --- | --- |\n| PaSa | 1.0 |",
            ["Experiments", "Setup"],
            [LOCATOR],
            semantic_heading_path=["Experiments", "Results"],
        )
        paper = paper_with([table])
        markdown, chunks = build_chunks(paper.elements, ChunkingConfig())
        chunks[0].retrieval_text = chunks[0].content_text

        validate_rag_quality(paper, chunks, markdown, ChunkingConfig())

        self.assertIn(
            "rag_retrieval_heading_mismatch", {item.code for item in paper.diagnostics}
        )

    def test_detects_duplicate_element_ids_and_bad_offsets(self) -> None:
        paper = paper_with(
            [
                ParsedElement(
                    "duplicate", "paragraph", "First paragraph.", ["Method"], [LOCATOR]
                ),
                ParsedElement(
                    "duplicate", "paragraph", "Second paragraph.", ["Method"], [LOCATOR]
                ),
            ]
        )

        self.validate(paper)

        codes = {item.code for item in paper.diagnostics}
        self.assertIn("rag_element_id_duplicate", codes)
        self.assertIn("rag_chunk_span_mismatch", codes)

    def test_detects_incomplete_label_graph(self) -> None:
        paper = paper_with(
            [ParsedElement("heading", "heading", "## Method", ["Method"], [LOCATOR])],
            source_labels=["sec:method"],
            source_reference_keys=["sec:method"],
        )

        self.validate(paper)

        codes = {item.code for item in paper.diagnostics}
        self.assertIn("rag_label_graph_incomplete", codes)
        self.assertIn("rag_cross_reference_unresolved", codes)
        self.assertIn("rag_reference_edges_incomplete", codes)

    def test_detects_control_characters_and_bibliography_fragments(self) -> None:
        paper = paper_with(
            [
                ParsedElement(
                    "control",
                    "paragraph",
                    "Policy loss\x10 fragment.",
                    ["Method"],
                    [LOCATOR],
                ),
                ParsedElement(
                    "reference",
                    "bibliography",
                    "arXiv:2303.08774.",
                    ["References"],
                    [LOCATOR],
                ),
            ]
        )

        self.validate(paper)

        codes = {item.code for item in paper.diagnostics}
        self.assertIn("rag_control_character", codes)
        self.assertIn("rag_bibliography_fragment", codes)

    def test_detects_unparsed_appendix_subheading(self) -> None:
        paper = paper_with(
            [
                ParsedElement(
                    "appendix",
                    "paragraph",
                    "B.1 Annotation Instructions",
                    ["B Annotation Details"],
                    [LOCATOR],
                )
            ]
        )

        self.validate(paper)

        self.assertIn(
            "rag_appendix_heading_unparsed",
            {item.code for item in paper.diagnostics},
        )

"""RAG chunk construction tests."""

import unittest

from athena.research.literature.contracts import ChunkingConfig
from athena.research.literature.paper_markdown.chunking import build_chunks
from athena.research.literature.paper_markdown.document import ParsedElement
from athena.research.literature.paper_markdown.schemas import (
    ElementKind,
    SourceLocator,
)


def element(
    identifier: str, kind: ElementKind, text: str, path: list[str]
) -> ParsedElement:
    """Create a traceable ParsedElement for chunk tests."""
    return ParsedElement(
        element_id=identifier,
        kind=kind,
        markdown=text,
        heading_path=path,
        locators=[
            SourceLocator(source_kind="tex", file="main.tex", line_start=1, line_end=1)
        ],
    )


class ChunkingTest(unittest.TestCase):
    def test_section_boundaries_start_new_chunks(self) -> None:
        elements = [
            element("h1", "heading", "## First", ["First"]),
            element("p1", "paragraph", "A" * 200, ["First"]),
            element("h2", "heading", "## Second", ["Second"]),
            element("p2", "paragraph", "B" * 200, ["Second"]),
        ]

        markdown, chunks = build_chunks(
            elements, ChunkingConfig(target_chars=1000, overlap_elements=1)
        )

        self.assertEqual(2, len(chunks))
        self.assertIn("## First", chunks[0].content_text)
        self.assertIn("## Second", chunks[1].content_text)
        self.assertNotIn("A" * 50, chunks[1].content_text)
        self.assertEqual(
            markdown[chunks[0].char_start : chunks[0].char_end], chunks[0].content_text
        )

    def test_size_split_repeats_only_complete_trailing_elements(self) -> None:
        elements = [
            element("h", "heading", "## Method", ["Method"]),
            element("p1", "paragraph", "A" * 700, ["Method"]),
            element("p2", "paragraph", "B" * 700, ["Method"]),
        ]

        _, chunks = build_chunks(
            elements, ChunkingConfig(target_chars=1000, overlap_elements=1)
        )

        self.assertEqual(2, len(chunks))
        self.assertIn("A" * 100, chunks[0].content_text)
        self.assertIn("A" * 100, chunks[1].content_text)
        self.assertIn("B" * 100, chunks[1].content_text)
        self.assertTrue(chunks[1].content_text.startswith("> Section: Method"))

    def test_visuals_citations_and_labels_are_indexed(self) -> None:
        item = element("table", "table", "| A |", ["Results"])
        item.visual_ids = ["table-1"]
        item.citation_keys = ["smith2024"]
        item.reference_keys = ["sec:method"]
        item.labels = ["tab:1"]
        item.semantic_heading_path = ["Experiments", "Results"]

        _, chunks = build_chunks([item], ChunkingConfig())

        self.assertEqual("table", chunks[0].kind)
        self.assertEqual(["table-1"], chunks[0].visual_ids)
        self.assertEqual(["smith2024"], chunks[0].citation_keys)
        self.assertEqual(["sec:method"], chunks[0].reference_keys)
        self.assertEqual(["tab:1"], chunks[0].labels)
        self.assertEqual(["Experiments", "Results"], chunks[0].semantic_heading_path)
        self.assertTrue(chunks[0].content_text.startswith("> Section: Results"))
        self.assertTrue(
            chunks[0].retrieval_text.startswith("> Section: Experiments / Results")
        )
        self.assertNotIn("> Section: Results\n", chunks[0].retrieval_text)

    def test_semantic_retrieval_text_replaces_a_grouped_source_heading(self) -> None:
        heading = element(
            "heading", "heading", "### Main results", ["Experiments", "Main results"]
        )
        table = element(
            "table",
            "table",
            "| Variant | Score |\n| --- | --- |\n| PaSa | 1.0 |",
            ["Experiments", "Main results"],
        )
        table.semantic_heading_path = ["Experiments", "Ablation study"]

        _, chunks = build_chunks([heading, table], ChunkingConfig())

        self.assertEqual(1, len(chunks))
        self.assertIn("### Main results", chunks[0].content_text)
        self.assertTrue(
            chunks[0].retrieval_text.startswith(
                "> Section: Experiments / Ablation study"
            )
        )
        self.assertNotIn("### Main results", chunks[0].retrieval_text)
        self.assertIn("| PaSa | 1.0 |", chunks[0].retrieval_text)

    def test_parent_heading_is_not_emitted_as_a_tiny_standalone_chunk(self) -> None:
        elements = [
            element("h1", "heading", "## Experiments", ["Experiments"]),
            element("h2", "heading", "### Setup", ["Experiments", "Setup"]),
            element("p", "paragraph", "Evaluation details.", ["Experiments", "Setup"]),
        ]

        _, chunks = build_chunks(elements, ChunkingConfig())

        self.assertEqual(1, len(chunks))
        self.assertIn("## Experiments", chunks[0].content_text)
        self.assertIn("### Setup", chunks[0].content_text)
        self.assertIn("Evaluation details.", chunks[0].content_text)

    def test_section_change_clears_overlap_for_non_heading_elements(self) -> None:
        bibliography = element(
            "b", "bibliography", "- [@smith] " + "R" * 700, ["References"]
        )
        bibliography.citation_keys = ["smith"]
        appendix_table = element(
            "t", "table", "| Appendix |\n| --- |\n| Value |", ["Appendix"]
        )
        appendix_table.visual_ids = ["table-appendix"]

        _, chunks = build_chunks(
            [bibliography, appendix_table],
            ChunkingConfig(target_chars=1000, overlap_elements=1),
        )

        self.assertEqual(2, len(chunks))
        self.assertEqual(["smith"], chunks[0].citation_keys)
        self.assertEqual([], chunks[1].citation_keys)
        self.assertNotIn("[@smith]", chunks[1].content_text)
        self.assertEqual(["Appendix"], chunks[1].heading_path)

    def test_non_prose_elements_are_not_repeated_on_size_split(self) -> None:
        table = element("t", "table", "T" * 700, ["Results"])
        paragraph = element("p", "paragraph", "P" * 700, ["Results"])

        _, chunks = build_chunks(
            [table, paragraph],
            ChunkingConfig(target_chars=1000, overlap_elements=1),
        )

        self.assertEqual(2, len(chunks))
        self.assertIn("T" * 100, chunks[0].content_text)
        self.assertNotIn("T" * 100, chunks[1].content_text)
        self.assertIn("P" * 100, chunks[1].content_text)

    def test_each_visual_element_gets_an_independent_chunk(self) -> None:
        heading = element("h", "heading", "## Results", ["Results"])
        first = element("t1", "table", "| First |\n| --- |\n| 1 |", ["Results"])
        first.visual_ids = ["table-1"]
        second = element("t2", "table", "| Second |\n| --- |\n| 2 |", ["Results"])
        second.visual_ids = ["table-2"]

        _, chunks = build_chunks([heading, first, second], ChunkingConfig())

        self.assertEqual(2, len(chunks))
        self.assertEqual(
            [["table-1"], ["table-2"]], [chunk.visual_ids for chunk in chunks]
        )
        self.assertIn("## Results", chunks[0].content_text)

    def test_each_bibliography_entry_gets_an_independent_chunk(self) -> None:
        heading = element("h", "heading", "## References", ["References"])
        entries = []
        for key in ("first", "second", "third"):
            entry = element(
                key, "bibliography", f"- [@{key}] Reference {key}.", ["References"]
            )
            entry.citation_keys = [key]
            entries.append(entry)

        _, chunks = build_chunks([heading, *entries], ChunkingConfig())

        self.assertEqual(3, len(chunks))
        self.assertEqual(
            [["first"], ["second"], ["third"]],
            [chunk.citation_keys for chunk in chunks],
        )
        self.assertIn("## References", chunks[0].content_text)
        self.assertNotIn("@first", chunks[1].content_text)

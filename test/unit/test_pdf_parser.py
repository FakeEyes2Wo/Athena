"""PyMuPDF fallback parser tests."""

import unittest

import fitz

from athena.research.paper_markdown.pdf_parser import (
    PdfBlock,
    citation_keys,
    display_reference_keys,
    heading_level,
    markdown_table,
    parse_pdf_paper,
    reading_order,
    split_pdf_author_line,
    text_from_block,
)


def png_bytes() -> bytes:
    """Create a valid in-memory PNG with PyMuPDF."""
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16), False)
    pixmap.clear_with(180)
    return pixmap.tobytes("png")


def simple_pdf() -> bytes:
    """Create a one-page paper-like PDF."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A Small Paper", fontsize=20)
    page.insert_text((72, 110), "Abstract", fontsize=14)
    page.insert_textbox(
        (72, 125, 500, 180),
        "This paper studies retrieval augmented generation and preserves citations [1].",
        fontsize=10,
    )
    page.insert_text((72, 210), "1 Introduction", fontsize=14)
    page.insert_textbox(
        (72, 225, 500, 300),
        "The method converts source documents into searchable Markdown chunks with provenance.",
        fontsize=10,
    )
    payload = document.tobytes()
    document.close()
    return payload


class PdfParserTest(unittest.TestCase):
    def test_pdf_text_removes_unindexable_control_and_private_use_characters(
        self,
    ) -> None:
        text, _, _, _, _ = text_from_block(
            {
                "lines": [
                    {
                        "spans": [
                            {
                                "text": "loss\x10 \uf8f1value",
                                "size": 10,
                                "font": "Regular",
                            }
                        ]
                    }
                ]
            }
        )

        self.assertEqual("loss value", text)

    def test_author_year_and_display_reference_groups_are_expanded(self) -> None:
        self.assertEqual(
            [
                "Kingsley et al., 2011",
                "Gusenbauer and Haddaway, 2021",
                "Gusenbauer and Haddaway, 2020",
            ],
            citation_keys(
                "Prior work (Kingsley et al., 2011; Gusenbauer and Haddaway, 2021, 2020)."
            ),
        )
        self.assertEqual(
            ["Table 18", "Table 19", "Table 20", "Figure 3"],
            display_reference_keys(
                "Tables are shown in Table 18, 19 and 20 and Fig. 3."
            ),
        )

    def test_appendix_subheading_uses_nested_heading_level(self) -> None:
        block = PdfBlock(
            1,
            (72, 100, 300, 120),
            "text",
            text="B.1 Annotation Instructions",
            max_font_size=10,
            bold=True,
        )

        self.assertEqual(2, heading_level(block, 10, [14, 10]))

    def test_pdf_author_footnotes_split_names(self) -> None:
        self.assertEqual(
            ["Yichen He", "Guanhua Huang", "Peiyuan Feng", "Yuan Lin"],
            split_pdf_author_line(
                "Yichen He∗1 Guanhua Huang∗1 Peiyuan Feng1 Yuan Lin†1"
            ),
        )

    def test_recovers_title_abstract_headings_and_pdf_locators(self) -> None:
        paper = parse_pdf_paper(simple_pdf())
        markdown = "\n\n".join(element.markdown for element in paper.elements)

        self.assertEqual("A Small Paper", paper.title)
        self.assertIn("retrieval augmented generation", paper.abstract)
        self.assertTrue(any(element.kind == "abstract" for element in paper.elements))
        self.assertIn("## 1 Introduction", markdown)
        self.assertTrue(
            all(element.locators[0].page_number == 1 for element in paper.elements)
        )
        self.assertTrue(
            all(element.locators[0].bbox is not None for element in paper.elements)
        )

    def test_reference_section_uses_bibliography_elements(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Reference Types", fontsize=20)
        page.insert_text((72, 120), "References", fontsize=14)
        page.insert_textbox(
            (72, 140, 500, 220),
            "[1] Alice Smith. A retrieval paper with sufficient bibliographic details. 2026.",
            fontsize=10,
        )

        paper = parse_pdf_paper(document.tobytes())
        document.close()

        bibliography = [
            element for element in paper.elements if element.kind == "bibliography"
        ]
        self.assertEqual(1, len(bibliography))
        self.assertIn("Alice Smith", paper.bibliography)

    def test_reference_blocks_are_merged_into_complete_entries(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Reference Continuations", fontsize=20)
        page.insert_text((72, 120), "References", fontsize=14)
        page.insert_textbox((72, 145, 500, 170), "Alice Smith. 2024.", fontsize=10)
        page.insert_textbox(
            (72, 180, 500, 215),
            "A retrieval paper with sufficient bibliographic details. Journal of Search.",
            fontsize=10,
        )
        page.insert_textbox(
            (72, 225, 500, 270),
            "Bob Jones. 2025. Another complete retrieval reference. Journal of RAG.",
            fontsize=10,
        )

        paper = parse_pdf_paper(document.tobytes())
        document.close()
        bibliography = [
            element for element in paper.elements if element.kind == "bibliography"
        ]

        self.assertEqual(2, len(bibliography))
        self.assertIn("Alice Smith. 2024. A retrieval paper", bibliography[0].markdown)

    def test_visual_semantic_heading_uses_explicit_display_reference(self) -> None:
        document = fitz.open()
        first = document.new_page()
        first.insert_text((72, 72), "Reference Context Paper", fontsize=20)
        first.insert_text((72, 120), "1 Results", fontsize=14)
        first.insert_textbox(
            (72, 140, 500, 230),
            "The quantitative comparison in Table 1 provides the principal retrieval result with sufficient explanatory context.",
            fontsize=10,
        )
        second = document.new_page()
        second.insert_text((72, 72), "2 Dataset", fontsize=14)
        second.insert_textbox(
            (72, 90, 500, 135),
            "This section contains enough prose to keep deterministic PDF extraction active on the page.",
            fontsize=10,
        )
        for x in (72, 250, 428):
            second.draw_line((x, 160), (x, 240))
        for y in (160, 200, 240):
            second.draw_line((72, y), (428, y))
        second.insert_text((90, 185), "Method", fontsize=10)
        second.insert_text((270, 185), "Score", fontsize=10)
        second.insert_text((90, 225), "Athena", fontsize=10)
        second.insert_text((270, 225), "0.91", fontsize=10)
        second.insert_text((72, 260), "Table 1: Dataset comparison.", fontsize=10)

        paper = parse_pdf_paper(document.tobytes())
        document.close()
        table = next(element for element in paper.elements if element.kind == "table")
        reference = next(
            element
            for element in paper.elements
            if "principal retrieval result" in element.markdown
        )

        self.assertEqual(["Table 1"], reference.reference_keys)
        self.assertEqual(["2 Dataset"], table.heading_path)
        self.assertEqual(["1 Results"], table.semantic_heading_path)
        self.assertEqual(["Table 1"], paper.source_reference_keys)

    def test_two_column_reading_order_keeps_left_column_before_right(self) -> None:
        left_low = PdfBlock(1, (40, 200, 250, 240), "text", text="left 2")
        right_high = PdfBlock(1, (320, 100, 550, 140), "text", text="right 1")
        left_high = PdfBlock(1, (40, 100, 250, 140), "text", text="left 1")
        right_low = PdfBlock(1, (320, 200, 550, 240), "text", text="right 2")

        ordered = reading_order([right_high, left_low, right_low, left_high], 600)

        self.assertEqual(
            ["left 1", "left 2", "right 1", "right 2"], [item.text for item in ordered]
        )

    def test_repeated_headers_and_footers_are_removed(self) -> None:
        document = fitz.open()
        for number in range(1, 4):
            page = document.new_page()
            page.insert_text((72, 30), "Conference 2026", fontsize=8)
            page.insert_textbox(
                (72, 100, 500, 220),
                f"Page-specific research evidence number {number} with enough prose to remain extractable and useful for retrieval indexing.",
                fontsize=10,
            )
            page.insert_text((280, 810), str(number), fontsize=8)
        paper = parse_pdf_paper(document.tobytes())
        document.close()
        markdown = "\n\n".join(element.markdown for element in paper.elements)

        self.assertNotIn("Conference 2026", markdown)
        self.assertIn("Page-specific research evidence", markdown)

    def test_embedded_figure_becomes_visual_with_crop_and_caption(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_textbox(
            (72, 60, 500, 120),
            "This page contains sufficient surrounding discussion so deterministic text extraction remains active.",
            fontsize=10,
        )
        page.insert_image((100, 160, 450, 360), stream=png_bytes())
        page.insert_textbox(
            (100, 370, 450, 410), "Figure 1: Retrieval architecture.", fontsize=10
        )
        paper = parse_pdf_paper(document.tobytes())
        document.close()

        figure = next(visual for visual in paper.visuals if visual.kind == "figure")
        self.assertEqual("Figure 1", figure.label)
        self.assertEqual("Retrieval architecture.", figure.caption)
        self.assertIsNotNone(figure.preview_bytes)
        self.assertIn(
            figure.visual_id,
            next(
                element for element in paper.elements if element.kind == "figure"
            ).visual_ids,
        )

    def test_image_only_page_is_delegated_as_full_page_visual(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_image(page.rect, stream=png_bytes())
        paper = parse_pdf_paper(document.tobytes())
        document.close()

        self.assertEqual(["page"], [visual.kind for visual in paper.visuals])
        self.assertIn(
            "pdf_page_requires_visual_ocr", {item.code for item in paper.diagnostics}
        )

    def test_low_text_page_preserves_extracted_text_as_visual_evidence(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Sparse but useful retrieval evidence.", fontsize=10)

        paper = parse_pdf_paper(document.tobytes())
        document.close()

        self.assertEqual(["page"], [visual.kind for visual in paper.visuals])
        self.assertIn(
            "Sparse but useful retrieval evidence.",
            paper.visuals[0].structured_text or "",
        )

    def test_ruled_pdf_table_becomes_markdown_and_visual_region(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_textbox(
            (72, 40, 500, 100),
            "This page contains enough explanatory prose before the quantitative results table for retrieval.",
            fontsize=10,
        )
        for x in (72, 250, 428):
            page.draw_line((x, 150), (x, 230))
        for y in (150, 190, 230):
            page.draw_line((72, y), (428, y))
        page.insert_text((90, 175), "Method", fontsize=10)
        page.insert_text((270, 175), "Score", fontsize=10)
        page.insert_text((90, 215), "Athena", fontsize=10)
        page.insert_text((270, 215), "0.91", fontsize=10)
        page.insert_text((72, 250), "Table 1: Results.", fontsize=10)

        paper = parse_pdf_paper(document.tobytes())
        document.close()
        table = next(visual for visual in paper.visuals if visual.kind == "table")

        self.assertEqual("Table 1", table.label)
        self.assertIn("| Athena | 0.91 |", table.structured_text or "")
        self.assertIsNotNone(table.preview_bytes)

    def test_adjacent_caption_regions_do_not_reuse_previous_visual(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_textbox(
            (72, 40, 500, 90),
            "This page contains sufficient surrounding prose for deterministic table extraction and visual region inference.",
            fontsize=10,
        )
        for x in (72, 250, 428):
            page.draw_line((x, 130), (x, 200))
        for y in (130, 165, 200):
            page.draw_line((72, y), (428, y))
        page.insert_text((90, 150), "Method", fontsize=10)
        page.insert_text((270, 150), "Score", fontsize=10)
        page.insert_text((90, 185), "Athena", fontsize=10)
        page.insert_text((270, 185), "0.91", fontsize=10)
        page.insert_text((72, 220), "Table 1: Results.", fontsize=10)
        page.insert_textbox(
            (72, 250, 428, 290),
            "Metric Value A 0.8 B 0.9 C 1.0",
            fontsize=10,
        )
        page.insert_text((72, 310), "Table 2: Text results.", fontsize=10)
        page.insert_textbox(
            (72, 340, 428, 390),
            "More prose after the tables keeps this page active for the parser.",
            fontsize=10,
        )

        paper = parse_pdf_paper(document.tobytes())
        document.close()
        tables = [visual for visual in paper.visuals if visual.kind == "table"]

        self.assertEqual(["Table 1", "Table 2"], [table.label for table in tables])
        self.assertLess(tables[0].locator.bbox[3], tables[1].locator.bbox[1])

    def test_markdown_table_preserves_empty_cells_and_escapes_pipes(self) -> None:
        markdown = markdown_table([["Metric", "Value"], ["A|B", None]])

        self.assertIn("| Metric | Value |", markdown)
        self.assertIn("| A\\|B |  |", markdown)

    def test_standalone_pdf_equation_is_preserved_without_inventing_tex(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_textbox(
            (72, 60, 500, 130),
            "The objective is defined below and the surrounding prose remains available for retrieval evidence.",
            fontsize=10,
        )
        page.insert_textbox(
            (150, 160, 450, 200),
            "L = sum_i x_i^2",
            fontsize=11,
            align=fitz.TEXT_ALIGN_CENTER,
        )
        paper = parse_pdf_paper(document.tobytes())
        document.close()
        equation = next(
            element for element in paper.elements if element.kind == "equation"
        )

        self.assertIn("```math", equation.markdown)
        self.assertIn("L = sum_i x_i^2", equation.markdown)
        self.assertEqual(1, len(paper.visuals))
        self.assertEqual("equation", paper.visuals[0].kind)
        self.assertIn(paper.visuals[0].visual_id, equation.visual_ids)
        self.assertIn(
            "pdf_equation_text_only", {item.code for item in paper.diagnostics}
        )

    def test_inline_equation_stays_in_text_without_visual_task(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Inline Equation Paper", fontsize=20)
        page.insert_textbox(
            (72, 110, 520, 180),
            "The loss L = sum_i x_i^2 is optimized directly from the retrieved evidence.",
            fontsize=10,
            align=fitz.TEXT_ALIGN_CENTER,
        )
        paper = parse_pdf_paper(document.tobytes())
        document.close()

        self.assertEqual([], paper.visuals)
        self.assertIn(
            "L = sum_i x_i^2", "\n".join(element.markdown for element in paper.elements)
        )

    def test_invalid_pdf_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_pdf_paper(b"not a pdf")

"""Deterministic paper RAG quality-gate tests."""

import pathlib
import re
import unittest

import athena.research.paper_markdown

from athena.research.paper_markdown.chunking import build_chunks
from athena.research.paper_markdown.document import ParsedElement, ParsedPaper
from athena.research.paper_markdown.quality import (
    ALIGNMENT_ENVIRONMENTS,
    BOOKKEEPING_CODES,
    CONTENT_LOSS_CODES,
    grade_quality,
)
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


class QualityGradingTest(unittest.TestCase):
    """`degraded` must mean content is missing, not merely that a check fired.

    The old rule was `"degraded" if quality_codes else "pass"`, which gave the same
    label to "the whole visual modality is absent" and "one chunk is 40% over the
    target size". Downstream could not tell whether re-running would help.
    """

    def test_no_codes_is_a_clean_pass(self):
        self.assertEqual("pass", grade_quality([]))

    def test_bookkeeping_only_is_not_degraded(self):
        self.assertEqual(
            "pass_with_notes",
            grade_quality(["rag_chunk_oversized", "rag_chunk_multiple_visuals"]),
        )

    def test_missing_visual_interpretation_is_degraded(self):
        self.assertEqual(
            "degraded", grade_quality(["visual_interpretation_unavailable"])
        )

    def test_content_loss_outranks_bookkeeping(self):
        self.assertEqual(
            "degraded",
            grade_quality(["rag_chunk_oversized", "visual_interpretation_failed"]),
        )

    def test_unregistered_codes_are_treated_conservatively(self):
        self.assertEqual("degraded", grade_quality(["some_brand_new_code"]))

    def test_every_emitted_code_is_classified(self):
        """A new diagnostic code must be graded explicitly, not silently inherit one."""
        package = pathlib.Path(athena.research.paper_markdown.__file__).parent
        emitted = set()
        for path in package.glob("*.py"):
            emitted.update(
                re.findall(r'code="([a-z0-9_]+)"', path.read_text(encoding="utf-8"))
            )
        classified = CONTENT_LOSS_CODES | BOOKKEEPING_CODES
        self.assertTrue(emitted, "no diagnostic codes were discovered")
        self.assertEqual(
            set(), emitted - classified, "these codes are not graded anywhere"
        )

    def test_the_two_classes_do_not_overlap(self):
        self.assertEqual(set(), CONTENT_LOSS_CODES & BOOKKEEPING_CODES)


class MathAlignmentTest(unittest.TestCase):
    """公式对齐环境的识别 —— 漏一个环境就会把合法公式判成内容缺失。

    LaTeX 片段一律用 raw 字符串：``"\\begin"`` 里的 ``\\b`` 是退格符，
    写成非 raw 会同时触发 ``rag_control_character``，把测试意图搅浑。
    """

    def codes(self, markdown: str) -> list[str]:
        """跑一遍质量门禁，返回它给出的全部诊断码。"""
        paper = paper_with(
            [
                ParsedElement(
                    element_id="equation-1",
                    kind="equation",
                    markdown=markdown,
                    heading_path=["Method"],
                    locators=[LOCATOR],
                )
            ]
        )
        body, chunks = build_chunks(paper.elements, ChunkingConfig())
        validate_rag_quality(paper, chunks, body, ChunkingConfig())
        return [item.code for item in paper.diagnostics]

    def test_split_is_a_valid_alignment_environment(self):
        """真机命中：8 个用 split 的合法公式被判成不支持的对齐标记。

        split 是标准 amsmath 环境，与 aligned 一样合法、KaTeX 能正常渲染，正文一个字
        都没丢，却让三篇论文被判 degraded 挡在语料之外。
        """
        markdown = (
            "$$\n"
            r"\begin{split}"
            "\n"
            r"\mathcal{L} &\approx \sum_i g_i f(x_i) \\"
            "\n"
            r"& \propto \Omega(f)"
            "\n"
            r"\end{split}"
            "\n$$"
        )

        self.assertNotIn("rag_math_markdown_invalid", self.codes(markdown))

    def test_every_alignment_environment_is_recognized(self):
        for name in ALIGNMENT_ENVIRONMENTS:
            markdown = (
                "$$\n"
                + rf"\begin{{{name}}}"
                + "\na &= b\n"
                + rf"\end{{{name}}}"
                + "\n$$"
            )
            with self.subTest(environment=name):
                self.assertNotIn("rag_math_markdown_invalid", self.codes(markdown))

    def test_matrix_family_is_recognized(self):
        for name in ("matrix", "bmatrix", "pmatrix", "smallmatrix"):
            markdown = (
                "$$\n"
                + rf"\begin{{{name}}}"
                + "\na & b\n"
                + rf"\end{{{name}}}"
                + "\n$$"
            )
            with self.subTest(environment=name):
                self.assertNotIn("rag_math_markdown_invalid", self.codes(markdown))

    def test_starred_variants_are_recognized(self):
        markdown = "$$\n" r"\begin{align*}" "\na &= b\n" r"\end{align*}" "\n$$"

        self.assertNotIn("rag_math_markdown_invalid", self.codes(markdown))

    def test_bare_ampersand_without_an_environment_is_still_flagged(self):
        """真正的裸 & 仍要报出来 —— 放宽识别不等于关掉这条检查。"""
        self.assertIn("rag_math_markdown_invalid", self.codes("$$\na &= b\n$$"))

    def test_leftover_label_is_still_flagged(self):
        markdown = (
            "$$\n" r"\begin{aligned}" "\na &= b\n" r"\end{aligned}\label{eq:1}" "\n$$"
        )

        self.assertIn("rag_math_markdown_invalid", self.codes(markdown))

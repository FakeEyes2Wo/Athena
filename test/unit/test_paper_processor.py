"""End-to-end paper processing, model assistance, and tool tests."""

import asyncio
import io
import tarfile
import tempfile
import unittest

import fitz

from athena.core.tool import ToolRegistry
from athena.core.tool_types import TOOL_BEGIN, TOOL_END, ToolContext
from athena.research.paper_markdown.document import ParsedElement, ParsedPaper
from athena.research.paper_markdown.interfaces import (
    StructureRepairResult,
    VisualInterpretation,
)
from athena.research.paper_markdown.processor import (
    PaperProcessor,
    VisualInterpretationRequiredError,
)
from athena.research.paper_markdown.schemas import (
    PaperContent,
    PaperConversionRequest,
    SourceLocator,
)
from athena.research.paper_markdown.tool import PaperMarkdownTool
from athena.storage import LocalArtifactStore

PLAIN_TEX = rb"""\documentclass{article}
\title{TeX Wins}\author{Ada \and Lin}
\begin{document}\maketitle\section{Intro}Source-first searchable evidence.\end{document}"""
TABLE_TEX = rb"""\begin{document}\section{Results}
\begin{table}\caption{Ablation scores}\label{tab:a}
\begin{tabular}{lr}Variant & F1 \\ Base & 0.7 \\ Athena & 0.9 \\ \end{tabular}
\end{table}\end{document}"""


def png_bytes() -> bytes:
    """Create a valid source figure PNG."""
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16), False)
    pixmap.clear_with(120)
    return pixmap.tobytes("png")


def figure_source_package() -> bytes:
    """Create a TeX package with an original standalone figure asset."""
    tex = rb"""\begin{document}
\begin{figure}\includegraphics{figures/architecture.png}
\caption{Retrieval architecture}\label{fig:arch}\end{figure}
\end{document}"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in {
            "main.tex": tex,
            "figures/architecture.png": png_bytes(),
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def paper_pdf() -> bytes:
    """Create a text-only PDF suitable for fallback tests."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "PDF Fallback", fontsize=20)
    page.insert_textbox(
        (72, 110, 500, 200),
        "This PDF provides enough extractable prose for deterministic retrieval chunk construction.",
        fontsize=10,
    )
    payload = document.tobytes()
    document.close()
    return payload


def sparse_pdf() -> bytes:
    """Create a low-text PDF whose deterministic evidence must not be discarded."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Sparse but useful retrieval evidence.", fontsize=10)
    payload = document.tobytes()
    document.close()
    return payload


def equation_pdf() -> bytes:
    """Create a PDF with an isolated display equation for VLM routing tests."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Equation Paper", fontsize=20)
    page.insert_textbox(
        (72, 110, 500, 160),
        "The objective is defined below and the surrounding prose remains useful for retrieval.",
        fontsize=10,
    )
    page.insert_textbox(
        (150, 190, 450, 230),
        "L = sum_i x_i^2",
        fontsize=11,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    payload = document.tobytes()
    document.close()
    return payload


class FakeVisualInterpreter:
    def __init__(self, fail: bool = False) -> None:
        self.requests = []
        self.fail = fail

    async def interpret(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("model unavailable")
        return VisualInterpretation(
            summary="The ablation table compares model variants.",
            searchable_text="Athena reaches F1 0.9 while the base reaches F1 0.7.",
            structured_data={"best_variant": "Athena", "best_f1": 0.9},
            model="test-vlm@1",
            confidence=0.99,
        )


class FakeStructureRefiner:
    def __init__(self) -> None:
        self.requests = []

    async def repair(self, request):
        self.requests.append(request)
        return StructureRepairResult(
            markdown="Repaired readable text.",
            model="test-llm@1",
            notes="restored spaces",
        )


class PaperProcessorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LocalArtifactStore(self.temporary.name)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def request_for_tex(
        self, payload: bytes = PLAIN_TEX, **values
    ) -> PaperConversionRequest:
        ref = await self.store.put_bytes(payload)
        return PaperConversionRequest(
            tex_source_ref=ref, tex_source_format="plain", **values
        )

    async def test_tex_is_used_when_both_sources_are_present(self) -> None:
        tex_ref = await self.store.put_bytes(PLAIN_TEX)
        invalid_pdf_ref = await self.store.put_bytes(b"invalid pdf must not be opened")
        request = PaperConversionRequest(
            tex_source_ref=tex_ref,
            tex_source_format="plain",
            pdf_ref=invalid_pdf_ref,
        )

        content = await PaperProcessor(self.store, None).process(request)

        self.assertEqual("tex", content.provenance.source_kind)
        self.assertEqual("TeX Wins", content.title)
        self.assertIn("Source-first", await content.load_markdown(self.store))

    async def test_pdf_is_used_only_when_tex_is_absent(self) -> None:
        pdf_ref = await self.store.put_bytes(paper_pdf())

        content = await PaperProcessor(self.store, None).process(
            PaperConversionRequest(pdf_ref=pdf_ref)
        )

        self.assertEqual("pdf", content.provenance.source_kind)
        self.assertEqual("PDF Fallback", content.title)

    async def test_pdf_best_effort_keeps_sparse_extracted_text(self) -> None:
        pdf_ref = await self.store.put_bytes(sparse_pdf())

        content = await PaperProcessor(self.store, None).process(
            PaperConversionRequest(pdf_ref=pdf_ref, visual_policy="best_effort")
        )

        self.assertIn(
            "Sparse but useful retrieval evidence.",
            await content.load_markdown(self.store),
        )

    async def test_pdf_equation_is_sent_to_visual_interpreter(self) -> None:
        pdf_ref = await self.store.put_bytes(equation_pdf())
        interpreter = FakeVisualInterpreter()

        content = await PaperProcessor(self.store, interpreter).process(
            PaperConversionRequest(pdf_ref=pdf_ref, visual_policy="required")
        )
        units = await content.load_retrieval_units(self.store)

        self.assertEqual(
            ["equation"], [request.kind for request in interpreter.requests]
        )
        visual = content.visuals[0]
        self.assertEqual("equation", visual.kind)
        self.assertEqual("interpreted", visual.interpretation_status)
        self.assertTrue(any(unit.kind == "visual:equation" for unit in units))
        self.assertEqual("pass", content.quality_status)

    async def test_invalid_tex_does_not_silently_fall_back_to_pdf(self) -> None:
        tex_ref = await self.store.put_bytes(b"not-a-zip")
        pdf_ref = await self.store.put_bytes(paper_pdf())
        request = PaperConversionRequest(
            tex_source_ref=tex_ref,
            tex_source_format="zip",
            pdf_ref=pdf_ref,
        )

        with self.assertRaises(ValueError):
            await PaperProcessor(self.store, None).process(request)

    async def test_visual_interpreter_is_required_by_default(self) -> None:
        request = await self.request_for_tex(TABLE_TEX)

        with self.assertRaises(VisualInterpretationRequiredError):
            await PaperProcessor(self.store, None).process(request)

    async def test_visual_explanation_is_persisted_and_exposed_to_rag(self) -> None:
        interpreter = FakeVisualInterpreter()
        request = await self.request_for_tex(TABLE_TEX, paper_id="paper:1")

        content = await PaperProcessor(self.store, interpreter).process(request)
        markdown = await content.load_markdown(self.store)
        units = await content.load_retrieval_units(self.store)

        self.assertEqual(1, len(interpreter.requests))
        self.assertEqual("table", interpreter.requests[0].kind)
        self.assertIn("Athena reaches F1 0.9", markdown)
        visual_unit = next(unit for unit in units if unit.kind == "visual:table")
        self.assertIn("F1 0.9", visual_unit.text)
        self.assertEqual(["Results"], visual_unit.heading_path)
        self.assertEqual("interpreted", visual_unit.metadata["interpretation_status"])
        self.assertTrue(visual_unit.metadata["chunk_ids"])
        for unit in units:
            self.assertTrue(unit.unit_id.startswith("paper:1:"))
            self.assertEqual("paper:1", unit.metadata["paper_id"])
            self.assertEqual("paper:1", unit.metadata["retrieval_namespace"])
            self.assertEqual(content.title, unit.metadata["title"])
            self.assertEqual(content.provenance.source_ref, unit.metadata["source_ref"])
            self.assertEqual("tex", unit.metadata["source_kind"])
            self.assertEqual("pass", unit.metadata["quality_status"])
            self.assertIn("quality_codes", unit.metadata)
        chunk_unit = next(unit for unit in units if unit.kind == "table")
        self.assertEqual(content.chunks[0].chunk_id, chunk_unit.metadata["chunk_id"])
        self.assertEqual("tab:a", chunk_unit.metadata["labels"])
        self.assertTrue(chunk_unit.metadata["visual_ids"])
        self.assertIn("citation_keys", chunk_unit.metadata)
        self.assertEqual("paper:1", content.paper_id)
        self.assertEqual("1.3", content.schema_version)
        self.assertEqual([], await content.load_diagnostics(self.store))
        self.assertEqual("pass", content.quality_status)
        visual = content.visuals[0]
        self.assertEqual(["Results"], visual.heading_path)
        self.assertEqual("test-vlm@1", visual.interpretation_model)
        self.assertEqual("interpreted", visual.interpretation_status)
        self.assertEqual(1, len(visual.chunk_ids))
        self.assertEqual(visual.visual_id, visual_unit.metadata["visual_id"])

    async def test_retrieval_ids_fall_back_to_source_fingerprint_namespace(
        self,
    ) -> None:
        first_request = await self.request_for_tex(PLAIN_TEX)
        second_request = await self.request_for_tex(
            PLAIN_TEX + b"\n% different source revision"
        )

        first = await PaperProcessor(self.store, None).process(first_request)
        second = await PaperProcessor(self.store, None).process(second_request)
        first_units = await first.load_retrieval_units(self.store)
        second_units = await second.load_retrieval_units(self.store)

        self.assertNotEqual(
            first.provenance.source_fingerprint, second.provenance.source_fingerprint
        )
        self.assertEqual(
            first_units[0].metadata["chunk_id"], second_units[0].metadata["chunk_id"]
        )
        self.assertNotEqual(first_units[0].unit_id, second_units[0].unit_id)
        self.assertTrue(
            first_units[0].unit_id.startswith(first.provenance.source_fingerprint + ":")
        )

    async def test_retrieval_ids_prefer_distinct_paper_id_namespaces(self) -> None:
        source_ref = await self.store.put_bytes(PLAIN_TEX)
        processor = PaperProcessor(self.store, None)
        first = await processor.process(
            PaperConversionRequest(tex_source_ref=source_ref, paper_id="paper:one")
        )
        second = await processor.process(
            PaperConversionRequest(tex_source_ref=source_ref, paper_id="paper:two")
        )
        first_unit = (await first.load_retrieval_units(self.store))[0]
        second_unit = (await second.load_retrieval_units(self.store))[0]

        self.assertEqual(
            first_unit.metadata["chunk_id"], second_unit.metadata["chunk_id"]
        )
        self.assertNotEqual(first_unit.unit_id, second_unit.unit_id)
        self.assertTrue(first_unit.unit_id.startswith("paper:one:"))
        self.assertTrue(second_unit.unit_id.startswith("paper:two:"))

    async def test_schema_1_1_defaults_new_rag_linkage_fields(self) -> None:
        request = await self.request_for_tex(TABLE_TEX, visual_policy="best_effort")
        current = await PaperProcessor(self.store, None).process(request)
        payload = current.model_dump(mode="json")
        payload["schema_version"] = "1.1"
        for chunk in payload["chunks"]:
            chunk.pop("semantic_heading_path")
            chunk.pop("reference_keys")
            chunk.pop("retrieval_text_ref")
        for visual in payload["visuals"]:
            visual.pop("source_heading_path")

        legacy = PaperContent.model_validate(payload)

        self.assertEqual("1.1", legacy.schema_version)
        self.assertTrue(all(not chunk.semantic_heading_path for chunk in legacy.chunks))
        self.assertTrue(all(not chunk.reference_keys for chunk in legacy.chunks))
        self.assertTrue(
            all(chunk.retrieval_text_ref is None for chunk in legacy.chunks)
        )
        self.assertTrue(
            all(not visual.source_heading_path for visual in legacy.visuals)
        )

    async def test_schema_1_2_retrieval_falls_back_to_content_ref(self) -> None:
        request = await self.request_for_tex(PLAIN_TEX)
        current = await PaperProcessor(self.store, None).process(request)
        payload = current.model_dump(mode="json")
        payload["schema_version"] = "1.2"
        for chunk in payload["chunks"]:
            chunk.pop("retrieval_text_ref")

        legacy = PaperContent.model_validate(payload)
        units = await legacy.load_retrieval_units(self.store)

        self.assertEqual("1.2", legacy.schema_version)
        self.assertTrue(
            all(chunk.retrieval_text_ref is None for chunk in legacy.chunks)
        )
        self.assertEqual(
            await self.store.get_text(legacy.chunks[0].content_ref), units[0].text
        )

    async def test_tex_figure_prefers_original_asset_and_produces_vlm_preview(
        self,
    ) -> None:
        source_ref = await self.store.put_bytes(figure_source_package())
        interpreter = FakeVisualInterpreter()

        content = await PaperProcessor(self.store, interpreter).process(
            PaperConversionRequest(tex_source_ref=source_ref)
        )
        figure = content.visuals[0]

        self.assertEqual("figure", figure.kind)
        self.assertEqual(
            png_bytes(), await self.store.get_bytes(figure.asset_ref or "")
        )
        self.assertIsNotNone(figure.preview_ref)
        self.assertIsNotNone(interpreter.requests[0].asset_ref)
        self.assertEqual("image/png", interpreter.requests[0].media_type)
        self.assertEqual("image/png", figure.preview_media_type)
        self.assertIn("athena-artifact://", await content.load_markdown(self.store))

    async def test_best_effort_visual_policy_never_invents_missing_facts(self) -> None:
        request = await self.request_for_tex(TABLE_TEX, visual_policy="best_effort")

        content = await PaperProcessor(self.store, None).process(request)
        markdown = await content.load_markdown(self.store)
        diagnostics = await content.load_diagnostics(self.store)

        self.assertIn("| Athena | 0.9 |", markdown)
        self.assertEqual(1, markdown.count("| Athena | 0.9 |"))
        self.assertIn(
            "visual_interpretation_unavailable", {item.code for item in diagnostics}
        )
        self.assertEqual("degraded", content.quality_status)
        self.assertIn("visual_interpretation_unavailable", content.quality_codes)
        units = await content.load_retrieval_units(self.store)
        self.assertFalse(any(unit.kind == "visual:table" for unit in units))
        visual = content.visuals[0]
        self.assertEqual(["Results"], visual.heading_path)
        self.assertEqual("deterministic-evidence-only", visual.interpretation_model)
        self.assertEqual("unavailable", visual.interpretation_status)

    async def test_best_effort_deduplicates_caption_in_visual_search_text(self) -> None:
        source_ref = await self.store.put_bytes(figure_source_package())

        content = await PaperProcessor(self.store, None).process(
            PaperConversionRequest(
                tex_source_ref=source_ref,
                visual_policy="best_effort",
            )
        )
        markdown = await content.load_markdown(self.store)
        search_text = await self.store.get_text(content.visuals[0].search_text_ref)
        units = await content.load_retrieval_units(self.store)

        self.assertEqual(1, markdown.count("Retrieval architecture"))
        self.assertEqual(1, search_text.count("Retrieval architecture"))
        self.assertFalse(any(unit.kind.startswith("visual:") for unit in units))

    async def test_retrieval_exposes_semantic_heading_and_reference_edges(self) -> None:
        tex = rb"""\begin{document}
\section{Experiments}\subsection{Setup}
\begin{table}\caption{Main scores}\label{main_scores}
\begin{tabular}{lr}Method & Score \\ PaSa & 1.0 \\ \end{tabular}\end{table}
\subsection{Results}Table~\ref{main_scores} reports the result.
\end{document}"""
        request = await self.request_for_tex(
            tex, visual_policy="best_effort", paper_id="paper:semantic"
        )

        content = await PaperProcessor(self.store, None).process(request)
        units = await content.load_retrieval_units(self.store)
        table_unit = next(
            unit for unit in units if unit.metadata["labels"] == "main_scores"
        )
        reference_unit = next(
            unit for unit in units if unit.metadata["reference_keys"] == "main_scores"
        )
        table_chunk = next(
            chunk for chunk in content.chunks if chunk.labels == ["main_scores"]
        )
        reference_chunk = next(
            chunk for chunk in content.chunks if chunk.reference_keys == ["main_scores"]
        )

        self.assertEqual(["Experiments", "Setup"], table_chunk.heading_path)
        self.assertEqual(["Experiments", "Results"], table_chunk.semantic_heading_path)
        self.assertEqual(["Experiments", "Results"], table_unit.heading_path)
        self.assertEqual(
            "Experiments / Setup", table_unit.metadata["source_heading_path"]
        )
        self.assertTrue(table_unit.text.startswith("> Section: Experiments / Results"))
        self.assertNotIn("> Section: Experiments / Setup", table_unit.text)
        self.assertIsNotNone(table_chunk.retrieval_text_ref)
        self.assertNotEqual(table_chunk.content_ref, table_chunk.retrieval_text_ref)
        source_text = await self.store.get_text(table_chunk.content_ref)
        self.assertIn("## Experiments", source_text)
        self.assertIn("### Setup", source_text)
        self.assertEqual(["main_scores"], reference_chunk.reference_keys)
        self.assertEqual("main_scores", reference_unit.metadata["reference_keys"])
        self.assertNotIn(
            "rag_retrieval_heading_mismatch",
            {item.code for item in await content.load_diagnostics(self.store)},
        )

    async def test_best_effort_recovers_from_visual_model_failure(self) -> None:
        request = await self.request_for_tex(TABLE_TEX, visual_policy="best_effort")

        content = await PaperProcessor(
            self.store, FakeVisualInterpreter(fail=True)
        ).process(request)

        self.assertIn(
            "visual_interpretation_failed",
            {item.code for item in await content.load_diagnostics(self.store)},
        )

    async def test_structure_llm_runs_only_for_flagged_elements(self) -> None:
        refiner = FakeStructureRefiner()
        processor = PaperProcessor(self.store, None, refiner)
        locator = SourceLocator(source_kind="pdf", page_number=1, bbox=(0, 0, 10, 10))
        paper = ParsedPaper(
            source_kind="pdf",
            source_fingerprint="f" * 64,
            converter="test",
            title="",
            authors=[],
            abstract="",
            elements=[
                ParsedElement("clean", "paragraph", "Clean text.", [], [locator]),
                ParsedElement(
                    "damaged",
                    "paragraph",
                    "Damagedtext",
                    [],
                    [locator],
                    repair_issue_codes=["spacing"],
                ),
            ],
            visuals=[],
            diagnostics=[],
        )

        await processor.repair_elements(paper)

        self.assertEqual(1, len(refiner.requests))
        self.assertEqual("Repaired readable text.", paper.elements[1].markdown)
        self.assertEqual("Clean text.", paper.elements[0].markdown)
        self.assertEqual(2, len(paper.diagnostics[0].evidence_refs))

    async def test_tool_round_trip_and_content_addressed_result(self) -> None:
        request = await self.request_for_tex()
        request_ref = await self.store.put_text(request.model_dump_json())
        tool = PaperMarkdownTool(self.store, None)
        registry = ToolRegistry()
        registry.register(tool)
        events: list[str] = []

        async def emit(kind: str, _ref: str, _data: dict | None = None) -> None:
            events.append(kind)

        ctx = ToolContext(tool.spec.name, "paper-call", emit, asyncio.Event())

        first = await registry.resolve(tool.spec.name).ainvoke(
            ctx,
            request_ref=request_ref,
        )
        second = await registry.resolve(tool.spec.name).ainvoke(
            ctx,
            request_ref=request_ref,
        )
        first_ref = first.data["paper_content_ref"]
        second_ref = second.data["paper_content_ref"]
        content = PaperContent.model_validate_json(await self.store.get_text(first_ref))

        self.assertEqual(first_ref, second_ref)
        self.assertEqual("TeX Wins", content.title)
        self.assertEqual([first_ref], first.artifacts)
        self.assertEqual(["paper_markdown"], [spec.name for spec in registry.specs])
        self.assertEqual(
            [TOOL_BEGIN, TOOL_END, TOOL_BEGIN, TOOL_END],
            events,
        )

"""TeX-first/PyMuPDF-fallback 论文处理与持久化流水线。"""

import asyncio
import json

from athena.research.paper_markdown.chunking import build_chunks
from athena.research.paper_markdown.document import (
    VISUAL_TOKEN,
    ParsedPaper,
    ParsedVisual,
)
from athena.research.paper_markdown.interfaces import (
    StructureRefiner,
    StructureRepairRequest,
    VisualInterpretation,
    VisualInterpretationRequest,
    VisualInterpreter,
)
from athena.research.paper_markdown.pdf_parser import parse_pdf_paper
from athena.research.paper_markdown.quality import validate_rag_quality
from athena.research.paper_markdown.schemas import (
    PaperChunk,
    PaperContent,
    PaperConversionRequest,
    PaperProvenance,
    PaperVisual,
    ProcessingDiagnostic,
)
from athena.research.paper_markdown.tex_parser import parse_tex_paper
from athena.research.paper_markdown.tex_source import load_tex_source
from athena.research.paper_markdown.visuals import (
    fallback_interpretation,
    render_preview,
    visual_markdown,
)
from athena.storage.artifact_store import ArtifactStore

REPAIR_INSTRUCTION = (
    "Restore Markdown structure and word spacing only. Preserve every claim, qualifier, "
    "citation, formula, number, and source order. Do not summarize or add facts."
)
DEFAULT_VISUAL_CONCURRENCY = 6


class VisualInterpretationRequiredError(RuntimeError):
    """请求要求完整视觉解释，但没有可用解释器或模型调用失败。"""


class PaperProcessor:
    """把上游 source artifact 转成可直接服务 RAG 的 ``PaperContent``。"""

    def __init__(
        self,
        artifacts: ArtifactStore,
        visual_interpreter: VisualInterpreter | None,
        structure_refiner: StructureRefiner | None = None,
        visual_concurrency: int = DEFAULT_VISUAL_CONCURRENCY,
    ) -> None:
        self.artifacts = artifacts
        self.visual_interpreter = visual_interpreter
        self.structure_refiner = structure_refiner
        self.visual_concurrency = max(1, visual_concurrency)

    async def parse_source(
        self, request: PaperConversionRequest
    ) -> tuple[ParsedPaper, str]:
        """严格执行 TeX 优先；只有未提供 TeX 时才使用 PDF。"""
        if request.tex_source_ref is not None:
            payload = await self.artifacts.get_bytes(request.tex_source_ref)
            expanded = await asyncio.to_thread(
                load_tex_source,
                payload,
                request.tex_source_format,
                request.tex_entrypoint,
            )
            return (
                await asyncio.to_thread(parse_tex_paper, expanded),
                request.tex_source_ref,
            )
        if request.pdf_ref is None:
            raise ValueError("PDF source is required when no TeX source is provided.")
        payload = await self.artifacts.get_bytes(request.pdf_ref)
        return await asyncio.to_thread(parse_pdf_paper, payload), request.pdf_ref

    async def repair_elements(self, paper: ParsedPaper) -> None:
        """只对解析器明确标记的低置信元素调用结构修复 LLM。"""
        uncertain = [
            element for element in paper.elements if element.repair_issue_codes
        ]
        if not uncertain:
            return
        if self.structure_refiner is None:
            paper.diagnostics.append(
                ProcessingDiagnostic(
                    level="warning",
                    code="structure_refiner_unavailable",
                    message=f"{len(uncertain)} low-confidence elements were preserved without LLM repair.",
                )
            )
            return
        for element in uncertain:
            original_ref = await self.artifacts.put_text(element.markdown)
            result = await self.structure_refiner.repair(
                StructureRepairRequest(
                    content_ref=original_ref,
                    issue_codes=element.repair_issue_codes,
                    locators=element.locators,
                    instruction=REPAIR_INSTRUCTION,
                )
            )
            repaired = result.markdown.strip()
            if not repaired:
                raise ValueError(
                    f"Structure refiner returned empty Markdown for {element.element_id}."
                )
            repaired_ref = await self.artifacts.put_text(repaired)
            element.markdown = repaired
            paper.diagnostics.append(
                ProcessingDiagnostic(
                    level="info",
                    code="structure_repaired_by_llm",
                    message=f"Repaired {element.element_id} with {result.model}: {result.notes}",
                    locator=element.locators[0] if element.locators else None,
                    evidence_refs=[original_ref, repaired_ref],
                )
            )

    async def interpret_visual(
        self,
        visual: ParsedVisual,
        request: PaperConversionRequest,
        paper: ParsedPaper,
    ) -> tuple[PaperVisual, VisualInterpretation, list[ProcessingDiagnostic]]:
        """保存视觉证据，调用解释器，并持久化结构化结果。

        诊断以返回值交出而不是就地追加到 ``paper.diagnostics``：多张图表并发解释时，
        就地追加会让诊断顺序随完成先后变化，调用方按源顺序合并才能保持可复现。
        """
        notes: list[ProcessingDiagnostic] = []
        asset_ref = (
            await self.artifacts.put_bytes(visual.asset_bytes)
            if visual.asset_bytes is not None
            else None
        )
        preview = visual.preview_bytes
        if preview is None and visual.asset_bytes is not None:
            preview = await asyncio.to_thread(
                render_preview,
                visual.asset_bytes,
                visual.asset_media_type,
            )
        preview_ref = (
            await self.artifacts.put_bytes(preview) if preview is not None else None
        )
        structured_ref = (
            await self.artifacts.put_text(visual.structured_text)
            if visual.structured_text
            else None
        )
        context = "\n".join(
            value
            for value in (
                f"Kind: {visual.kind}",
                f"Label: {visual.label}" if visual.label else "",
                f"Caption: {visual.caption}" if visual.caption else "",
                visual.surrounding_text,
            )
            if value
        )
        model_request = VisualInterpretationRequest(
            visual_id=visual.visual_id,
            kind=visual.kind,
            asset_ref=preview_ref or asset_ref,
            media_type="image/png" if preview_ref else visual.asset_media_type,
            structured_text_ref=structured_ref,
            context_ref=await self.artifacts.put_text(context),
            locator=visual.locator,
        )
        if self.visual_interpreter is None:
            if request.visual_policy == "required":
                raise VisualInterpretationRequiredError(
                    f"Visual interpreter is required for {visual.visual_id} ({visual.kind})."
                )
            interpretation = fallback_interpretation(visual)
            notes.append(
                ProcessingDiagnostic(
                    level="warning",
                    code="visual_interpretation_unavailable",
                    message=f"Used evidence-only text for {visual.visual_id}.",
                    locator=visual.locator,
                )
            )
        else:
            try:
                interpretation = await self.visual_interpreter.interpret(model_request)
            except Exception as error:
                if request.visual_policy == "required":
                    raise VisualInterpretationRequiredError(
                        f"Visual interpretation failed for {visual.visual_id}: {error}"
                    ) from error
                interpretation = fallback_interpretation(visual)
                notes.append(
                    ProcessingDiagnostic(
                        level="warning",
                        code="visual_interpretation_failed",
                        message=(
                            f"Used evidence-only text after model failure for {visual.visual_id}: "
                            f"{error}"
                        ),
                        locator=visual.locator,
                    )
                )
        interpretation_ref = await self.artifacts.put_text(
            interpretation.model_dump_json()
        )
        search_text = "\n".join(
            dict.fromkeys(
                value
                for value in (
                    visual.label or "",
                    visual.caption,
                    interpretation.searchable_text.strip(),
                )
                if value
            )
        )
        status = (
            "unavailable"
            if interpretation.structured_data.get("interpretation_status")
            == "unavailable"
            else "interpreted"
        )
        stored = PaperVisual(
            visual_id=visual.visual_id,
            kind=visual.kind,
            element_id=visual.element_id,
            label=visual.label,
            caption=visual.caption,
            asset_ref=asset_ref,
            asset_media_type=visual.asset_media_type,
            preview_ref=preview_ref,
            preview_media_type="image/png" if preview_ref else None,
            structured_text_ref=structured_ref,
            interpretation_ref=interpretation_ref,
            interpretation_model=interpretation.model,
            interpretation_status=status,
            search_text_ref=await self.artifacts.put_text(search_text),
            locator=visual.locator,
        )
        return stored, interpretation, notes

    async def enrich_visuals(
        self,
        paper: ParsedPaper,
        request: PaperConversionRequest,
    ) -> list[PaperVisual]:
        """并发解释视觉资源，再按源顺序替换正文占位符。

        每次解释都是一次模型往返，串行执行会让转换耗时与图表数线性相关——实测一篇 22
        张图的论文要 1776 秒，而单次调用只有约 30 秒。解释之间没有依赖，因此并发发出，
        再按 ``paper.visuals`` 的原始顺序落地：占位符替换、heading 归属和诊断合并都在
        gather 之后串行完成，产物与串行版本逐字节一致。

        并发度受 ``visual_concurrency`` 约束——上游端点在并发下单次延迟会上升，无界并发
        既拿不到额外吞吐，也可能触发限流。
        """
        elements = {element.element_id: element for element in paper.elements}
        for visual in paper.visuals:
            if visual.element_id not in elements:
                raise ValueError(
                    f"Visual {visual.visual_id} references missing element {visual.element_id}."
                )

        gate = asyncio.Semaphore(self.visual_concurrency)

        async def interpret(visual: ParsedVisual):
            async with gate:
                return await self.interpret_visual(visual, request, paper)

        results = await asyncio.gather(*(interpret(visual) for visual in paper.visuals))

        stored_visuals: list[PaperVisual] = []
        for visual, (stored, interpretation, notes) in zip(
            paper.visuals, results, strict=True
        ):
            element = elements[visual.element_id]
            paper.diagnostics.extend(notes)
            stored.source_heading_path = list(element.heading_path)
            stored.heading_path = list(
                element.semantic_heading_path or element.heading_path
            )
            stored_visuals.append(stored)
            token = VISUAL_TOKEN.format(visual_id=visual.visual_id)
            if token not in element.markdown:
                raise ValueError(
                    f"Visual placeholder is missing from {element.element_id}: {visual.visual_id}"
                )
            element.markdown = element.markdown.replace(
                token,
                visual_markdown(visual, stored, interpretation),
            )
        return stored_visuals

    async def process(self, request: PaperConversionRequest) -> PaperContent:
        """完成解析、必要模型辅助、RAG chunking 和 artifact 持久化。"""
        paper, source_ref = await self.parse_source(request)
        await self.repair_elements(paper)
        visuals = await self.enrich_visuals(paper, request)
        markdown, draft_chunks = build_chunks(paper.elements, request.chunking)
        validate_rag_quality(paper, draft_chunks, markdown, request.chunking)
        for visual in visuals:
            visual.chunk_ids = [
                chunk.chunk_id
                for chunk in draft_chunks
                if visual.visual_id in chunk.visual_ids
            ]

        chunks: list[PaperChunk] = []
        for chunk in draft_chunks:
            chunks.append(
                PaperChunk(
                    chunk_id=chunk.chunk_id,
                    kind=chunk.kind,
                    content_ref=await self.artifacts.put_text(chunk.content_text),
                    retrieval_text_ref=await self.artifacts.put_text(
                        chunk.retrieval_text
                    ),
                    heading_path=chunk.heading_path,
                    semantic_heading_path=chunk.semantic_heading_path,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    token_estimate=chunk.token_estimate,
                    locators=chunk.locators,
                    citation_keys=chunk.citation_keys,
                    reference_keys=chunk.reference_keys,
                    labels=chunk.labels,
                    visual_ids=chunk.visual_ids,
                )
            )

        diagnostics_ref = await self.artifacts.put_text(
            json.dumps(
                [
                    diagnostic.model_dump(mode="json")
                    for diagnostic in paper.diagnostics
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        quality_codes = list(
            dict.fromkeys(
                diagnostic.code
                for diagnostic in paper.diagnostics
                if diagnostic.level in {"warning", "error"}
            )
        )
        return PaperContent(
            paper_id=request.paper_id,
            title=paper.title or request.metadata.get("title", ""),
            authors=paper.authors,
            metadata=request.metadata,
            provenance=PaperProvenance(
                source_kind=paper.source_kind,
                source_ref=source_ref,
                source_fingerprint=paper.source_fingerprint,
                tex_source_ref=request.tex_source_ref,
                pdf_ref=request.pdf_ref,
                converter=paper.converter,
            ),
            markdown_ref=await self.artifacts.put_text(markdown),
            abstract_ref=(
                await self.artifacts.put_text(paper.abstract)
                if paper.abstract
                else None
            ),
            bibliography_ref=(
                await self.artifacts.put_text(paper.bibliography)
                if paper.bibliography
                else None
            ),
            diagnostics_ref=diagnostics_ref,
            quality_status="degraded" if quality_codes else "pass",
            quality_codes=quality_codes,
            chunks=chunks,
            visuals=visuals,
        )

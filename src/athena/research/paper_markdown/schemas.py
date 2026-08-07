"""面向 RAG 的论文 Markdown 请求、索引与持久化模型。

模型把全文、chunk、视觉资源和解释文本分别保存为 artifact。``PaperContent`` 本身只
保留可查询的轻量索引与溯源信息，既避免大正文进入 Agent 状态，也允许检索器按 chunk
或视觉单元增量加载。
"""

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from athena.core.contracts import ArtifactRef

if TYPE_CHECKING:
    from athena.storage.artifact_store import ArtifactStore


SourceKind = Literal["tex", "pdf"]
ElementKind = Literal[
    "front_matter",
    "abstract",
    "heading",
    "paragraph",
    "list",
    "equation",
    "figure",
    "table",
    "code",
    "footnote",
    "bibliography",
]
VisualKind = Literal["figure", "table", "equation", "page"]
TexSourceFormat = Literal["auto", "tar", "tar.gz", "zip", "gzip", "plain"]
VisualPolicy = Literal["required", "best_effort"]
InterpretationStatus = Literal["interpreted", "unavailable", "unknown"]
QualityStatus = Literal["pass", "degraded", "unknown"]


class ChunkingConfig(BaseModel):
    """RAG chunk 的确定性大小策略。

    chunk 永不在单个结构元素中间截断；超长公式、表格或段落保持完整，以免损坏语义。
    """

    target_chars: int = Field(
        default=6000,
        ge=1000,
        le=30000,
        description="Preferred maximum chunk size in characters.",
    )
    overlap_elements: int = Field(
        default=1,
        ge=0,
        le=3,
        description="Number of complete trailing elements repeated in the next chunk.",
    )


class PaperConversionRequest(BaseModel):
    """上游交给论文处理器的输入引用。

    ``tex_source_ref`` 指向 tar/tar.gz/zip/gzip/plain TeX 源码包；``pdf_ref`` 指向 PDF
    字节。两者同时存在时正文始终走 TeX，PDF 只作为可记录的补充来源。
    """

    tex_source_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact holding a TeX source package supplied by the upstream search stage.",
    )
    tex_source_format: TexSourceFormat = Field(
        default="auto",
        description="Encoding of the TeX source artifact; auto detects common archive formats.",
    )
    tex_entrypoint: str | None = Field(
        default=None, description="Optional relative path of the root TeX file."
    )
    pdf_ref: ArtifactRef | None = Field(
        default=None, description="Artifact holding the upstream-provided paper PDF."
    )
    paper_id: str | None = Field(
        default=None,
        description="Canonical DOI, arXiv id, or corpus identifier when known.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Small upstream metadata such as title, venue, and year.",
    )
    visual_policy: VisualPolicy = Field(
        default="required",
        description="Whether missing visual interpretations fail processing or become diagnostics.",
    )
    chunking: ChunkingConfig = Field(
        default_factory=ChunkingConfig, description="RAG chunk construction settings."
    )

    @model_validator(mode="after")
    def require_source(self) -> "PaperConversionRequest":
        """拒绝没有 TeX 也没有 PDF 的请求。"""
        if self.tex_source_ref is None and self.pdf_ref is None:
            raise ValueError("At least one of tex_source_ref or pdf_ref is required.")
        return self


class SourceLocator(BaseModel):
    """一个内容元素在原始 TeX 或 PDF 中的位置。"""

    source_kind: SourceKind = Field(
        description="Source representation used for this locator."
    )
    file: str | None = Field(default=None, description="Relative TeX source path.")
    line_start: int | None = Field(
        default=None, ge=1, description="Inclusive 1-based source line."
    )
    line_end: int | None = Field(
        default=None, ge=1, description="Inclusive 1-based source line."
    )
    page_number: int | None = Field(
        default=None, ge=1, description="1-based PDF page number."
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="PDF rectangle in points: x0, y0, x1, y1."
    )


class ProcessingDiagnostic(BaseModel):
    """不会隐藏在日志中的解析质量事实。"""

    level: Literal["info", "warning", "error"] = Field(
        description="Diagnostic severity."
    )
    code: str = Field(description="Stable machine-readable diagnostic code.")
    message: str = Field(description="Human-readable diagnostic message.")
    locator: SourceLocator | None = Field(
        default=None, description="Source location related to the diagnostic."
    )
    evidence_refs: list[ArtifactRef] = Field(
        default_factory=list,
        description="Artifacts that substantiate the diagnostic or model-assisted change.",
    )


class PaperProvenance(BaseModel):
    """记录可用输入、实际采用通道和转换器版本。"""

    source_kind: SourceKind = Field(
        description="Primary source representation used to build the document."
    )
    source_ref: ArtifactRef = Field(description="Primary source artifact reference.")
    source_fingerprint: str = Field(
        description="SHA-256 hex digest of the primary source bytes."
    )
    tex_source_ref: ArtifactRef | None = Field(
        default=None, description="Provided TeX source artifact, if any."
    )
    pdf_ref: ArtifactRef | None = Field(
        default=None, description="Provided PDF artifact, if any."
    )
    converter: str = Field(description="Deterministic converter name and version.")


class PaperChunk(BaseModel):
    """一个可独立索引的 RAG 文本单元。"""

    chunk_id: str = Field(description="Stable content-derived chunk identifier.")
    kind: ElementKind = Field(description="Dominant semantic element type.")
    content_ref: ArtifactRef = Field(description="Artifact holding the chunk Markdown.")
    retrieval_text_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact holding semantic-section-normalized text for indexing; legacy chunks fall back to content_ref.",
    )
    heading_path: list[str] = Field(
        default_factory=list, description="Source-order section ancestry."
    )
    semantic_heading_path: list[str] = Field(
        default_factory=list,
        description="Reference-inferred section ancestry used for retrieval filtering when available.",
    )
    char_start: int = Field(
        ge=0, description="Inclusive offset in the persisted full Markdown."
    )
    char_end: int = Field(
        ge=0, description="Exclusive offset in the persisted full Markdown."
    )
    token_estimate: int = Field(
        ge=0, description="Provider-neutral approximate token count."
    )
    locators: list[SourceLocator] = Field(
        default_factory=list,
        description="Original source locations covered by this chunk.",
    )
    citation_keys: list[str] = Field(
        default_factory=list, description="Citation keys referenced by this chunk."
    )
    reference_keys: list[str] = Field(
        default_factory=list, description="Local labels referenced by this chunk."
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Section, equation, figure, or table labels defined by this chunk.",
    )
    visual_ids: list[str] = Field(
        default_factory=list, description="Visual assets discussed in this chunk."
    )


class PaperVisual(BaseModel):
    """图、表或 OCR 页面及其面向检索的解释。"""

    visual_id: str = Field(description="Stable identifier used by Markdown and chunks.")
    kind: VisualKind = Field(
        description="Figure, table, equation, or full page requiring visual reading."
    )
    element_id: str = Field(
        default="", description="Parsed element containing this visual."
    )
    heading_path: list[str] = Field(
        default_factory=list,
        description="Semantic section ancestry for retrieval filtering.",
    )
    source_heading_path: list[str] = Field(
        default_factory=list, description="Section ancestry at the source declaration."
    )
    chunk_ids: list[str] = Field(
        default_factory=list, description="Persisted chunks containing this visual."
    )
    label: str | None = Field(
        default=None, description="Original figure/table label when available."
    )
    caption: str = Field(default="", description="Caption recovered from the source.")
    asset_ref: ArtifactRef | None = Field(
        default=None, description="Original or cropped visual bytes."
    )
    asset_media_type: str | None = Field(
        default=None, description="Media type of the original or cropped asset."
    )
    preview_ref: ArtifactRef | None = Field(
        default=None, description="Normalized PNG preview supplied to a vision model."
    )
    preview_media_type: str | None = Field(
        default=None, description="Media type of the normalized preview."
    )
    structured_text_ref: ArtifactRef | None = Field(
        default=None, description="Deterministically extracted table or source text."
    )
    interpretation_ref: ArtifactRef = Field(
        description="Structured VisualInterpretation JSON artifact."
    )
    interpretation_model: str = Field(
        default="", description="Model or deterministic fallback identifier."
    )
    interpretation_status: InterpretationStatus = Field(
        default="unknown",
        description="Whether the visual has a model interpretation or only unavailable fallback evidence.",
    )
    search_text_ref: ArtifactRef = Field(
        description="Retrieval-oriented explanation text artifact."
    )
    locator: SourceLocator = Field(
        description="Original source location of the visual."
    )


class RetrievalUnit(BaseModel):
    """载入正文 chunk 或视觉解释后的统一检索输入。"""

    unit_id: str = Field(description="Paper-namespaced chunk or visual identifier.")
    kind: str = Field(description="Semantic retrieval unit type.")
    text: str = Field(description="Text to embed or index.")
    heading_path: list[str] = Field(
        default_factory=list, description="Section ancestry when applicable."
    )
    locators: list[SourceLocator] = Field(
        default_factory=list, description="Source evidence locations."
    )
    metadata: dict[str, str] = Field(
        default_factory=dict, description="Small retrieval filters and linkage fields."
    )


class PaperContent(BaseModel):
    """论文处理完成后的持久化索引。

    全文、chunk、诊断和视觉解释都独立内容寻址；本对象适合直接写入 StateStore 或
    ``PaperRecord.content``，并能按需恢复统一的 ``RetrievalUnit`` 列表。
    """

    schema_version: Literal["1.0", "1.1", "1.2", "1.3"] = Field(
        default="1.3", description="PaperContent schema version."
    )
    paper_id: str | None = Field(
        default=None, description="Canonical upstream paper identifier."
    )
    title: str = Field(default="", description="Recovered paper title.")
    authors: list[str] = Field(
        default_factory=list, description="Recovered author strings."
    )
    metadata: dict[str, str] = Field(
        default_factory=dict, description="Small upstream and recovered metadata."
    )
    provenance: PaperProvenance = Field(
        description="Source selection and converter provenance."
    )
    markdown_ref: ArtifactRef = Field(
        description="Artifact holding the complete normalized Markdown."
    )
    abstract_ref: ArtifactRef | None = Field(
        default=None, description="Abstract text artifact for fast access."
    )
    bibliography_ref: ArtifactRef | None = Field(
        default=None, description="Bibliography Markdown artifact when present."
    )
    diagnostics_ref: ArtifactRef = Field(
        description="ProcessingDiagnostic list JSON artifact."
    )
    quality_status: QualityStatus = Field(
        default="unknown",
        description="Pass for clean new outputs, degraded for warning/error diagnostics, unknown for legacy outputs.",
    )
    quality_codes: list[str] = Field(
        default_factory=list,
        description="Stable warning/error codes that an ingestion quality gate can evaluate without loading diagnostics.",
    )
    chunks: list[PaperChunk] = Field(description="Ordered RAG chunks.")
    visuals: list[PaperVisual] = Field(
        default_factory=list,
        description="Figures, tables, and visually recovered pages.",
    )

    async def load_markdown(self, store: "ArtifactStore") -> str:
        """读取完整 Markdown；例如 ``await content.load_markdown(store)``。"""
        return await store.get_text(self.markdown_ref)

    async def load_diagnostics(
        self, store: "ArtifactStore"
    ) -> list[ProcessingDiagnostic]:
        """读取结构化诊断，供质量门禁决定是否接纳论文。"""
        return TypeAdapter(list[ProcessingDiagnostic]).validate_json(
            await store.get_text(self.diagnostics_ref)
        )

    async def load_retrieval_units(self, store: "ArtifactStore") -> list[RetrievalUnit]:
        """载入正文 chunk 与独立视觉解释，形成可直接送入 RAG 索引器的单元。"""
        units: list[RetrievalUnit] = []
        namespace = self.paper_id or self.provenance.source_fingerprint
        common_metadata = {
            **self.metadata,
            "paper_id": self.paper_id or "",
            "title": self.title,
            "authors": "; ".join(self.authors),
            "source_kind": self.provenance.source_kind,
            "source_ref": self.provenance.source_ref,
            "quality_status": self.quality_status,
            "quality_codes": ",".join(self.quality_codes),
            "retrieval_namespace": namespace,
        }
        for chunk in self.chunks:
            retrieval_heading_path = chunk.semantic_heading_path or chunk.heading_path
            units.append(
                RetrievalUnit(
                    unit_id=f"{namespace}:{chunk.chunk_id}",
                    kind=chunk.kind,
                    text=await store.get_text(
                        chunk.retrieval_text_ref or chunk.content_ref
                    ),
                    heading_path=retrieval_heading_path,
                    locators=chunk.locators,
                    metadata={
                        **common_metadata,
                        "chunk_id": chunk.chunk_id,
                        "source_heading_path": " / ".join(chunk.heading_path),
                        "citation_keys": ",".join(chunk.citation_keys),
                        "reference_keys": ",".join(chunk.reference_keys),
                        "labels": ",".join(chunk.labels),
                        "visual_ids": ",".join(chunk.visual_ids),
                    },
                )
            )
        for visual in self.visuals:
            if visual.interpretation_status == "unavailable":
                continue
            units.append(
                RetrievalUnit(
                    unit_id=f"{namespace}:{visual.visual_id}",
                    kind=f"visual:{visual.kind}",
                    text=await store.get_text(visual.search_text_ref),
                    heading_path=visual.heading_path,
                    locators=[visual.locator],
                    metadata={
                        **common_metadata,
                        "visual_id": visual.visual_id,
                        "source_heading_path": " / ".join(visual.source_heading_path),
                        "label": visual.label or "",
                        "caption": visual.caption,
                        "element_id": visual.element_id,
                        "chunk_ids": ",".join(visual.chunk_ids),
                        "interpretation_model": visual.interpretation_model,
                        "interpretation_status": visual.interpretation_status,
                    },
                )
            )
        return units

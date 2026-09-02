"""TeX/PDF 解析器与持久化流水线之间的内存态文档。"""

from dataclasses import dataclass, field

from athena.research.literature.contracts import ProcessingDiagnostic
from athena.research.literature.paper_markdown.schemas import (
    ElementKind,
    SourceKind,
    SourceLocator,
    VisualKind,
)

VISUAL_TOKEN = "<!-- athena-visual:{visual_id} -->"


@dataclass(slots=True)
class ParsedElement:
    """一个未持久化但已结构化的论文元素。"""

    element_id: str
    kind: ElementKind
    markdown: str
    heading_path: list[str]
    locators: list[SourceLocator]
    citation_keys: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    visual_ids: list[str] = field(default_factory=list)
    repair_issue_codes: list[str] = field(default_factory=list)
    reference_keys: list[str] = field(default_factory=list)
    semantic_heading_path: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParsedVisual:
    """尚未落库的图、表或需要视觉 OCR 的整页。"""

    visual_id: str
    kind: VisualKind
    locator: SourceLocator
    element_id: str
    label: str | None = None
    caption: str = ""
    asset_bytes: bytes | None = None
    asset_media_type: str | None = None
    preview_bytes: bytes | None = None
    structured_text: str | None = None
    surrounding_text: str = ""


@dataclass(slots=True)
class ParsedPaper:
    """两个解析通道共享的完整内存态输出。"""

    source_kind: SourceKind
    source_fingerprint: str
    converter: str
    title: str
    authors: list[str]
    abstract: str
    elements: list[ParsedElement]
    visuals: list[ParsedVisual]
    diagnostics: list[ProcessingDiagnostic]
    bibliography: str = ""
    source_labels: list[str] = field(default_factory=list)
    source_reference_keys: list[str] = field(default_factory=list)

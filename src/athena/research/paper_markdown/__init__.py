"""TeX-first、PyMuPDF fallback 的 RAG 论文处理工具。"""

from athena.research.paper_markdown.interfaces import (
    StructureRepairRequest,
    StructureRepairResult,
    StructureRefiner,
    VisualInterpretation,
    VisualInterpretationRequest,
    VisualInterpreter,
)
from athena.research.paper_markdown.processor import (
    PaperProcessor,
    VisualInterpretationRequiredError,
)
from athena.research.paper_markdown.schemas import (
    PaperContent,
    PaperConversionRequest,
    PaperChunk,
    PaperVisual,
    RetrievalUnit,
)
from athena.research.paper_markdown.tool import PaperMarkdownTool

__all__ = [
    "PaperContent",
    "PaperConversionRequest",
    "PaperChunk",
    "PaperVisual",
    "RetrievalUnit",
    "VisualInterpretation",
    "VisualInterpretationRequest",
    "VisualInterpreter",
    "StructureRepairRequest",
    "StructureRepairResult",
    "StructureRefiner",
    "PaperProcessor",
    "PaperMarkdownTool",
    "VisualInterpretationRequiredError",
]

"""论文处理器可选 LLM 与必需视觉理解能力的供应商无关协议。"""

from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from athena.core.contracts import ArtifactRef
from athena.research.paper_markdown.schemas import SourceLocator, VisualKind


class VisualInterpretationRequest(BaseModel):
    """发送给独立 VLM 或 Athena 基底模型的受控视觉任务。"""

    visual_id: str = Field(description="Stable visual identifier.")
    kind: VisualKind = Field(
        description="Figure, table, equation, or page interpretation task."
    )
    asset_ref: ArtifactRef | None = Field(
        default=None, description="Original or normalized image artifact."
    )
    media_type: str | None = Field(
        default=None,
        description="Media type of asset_ref for provider request construction.",
    )
    structured_text_ref: ArtifactRef | None = Field(
        default=None, description="Extracted table/source text artifact."
    )
    context_ref: ArtifactRef = Field(
        description="Caption and nearby discussion artifact."
    )
    locator: SourceLocator = Field(description="Original evidence location.")

    @model_validator(mode="after")
    def require_evidence(self) -> "VisualInterpretationRequest":
        """视觉任务至少应包含图像或已抽取的表格文本。"""
        if self.asset_ref is None and self.structured_text_ref is None:
            raise ValueError(
                "Visual interpretation requires an asset or structured text."
            )
        return self


class VisualInterpretation(BaseModel):
    """视觉模型返回的结构化、可审计解释。"""

    summary: str = Field(description="Faithful explanation of the visual content.")
    searchable_text: str = Field(
        description="Self-contained text optimized for retrieval and embedding."
    )
    structured_data: dict[str, object] = Field(
        default_factory=dict,
        description="Axes, trends, table fields, or other machine-readable facts.",
    )
    model: str = Field(description="Model identifier and revision.")
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Optional calibrated confidence."
    )


class VisualInterpreter(Protocol):
    """由 VLM 或 Athena 基底模型实现的视觉解释接口。"""

    async def interpret(
        self, request: VisualInterpretationRequest
    ) -> VisualInterpretation:
        """解释单个图、表或低文本页面，不负责推进论文工作流。"""


class StructureRepairRequest(BaseModel):
    """只在确定性解析标记低置信时发送的 Markdown 修复任务。"""

    content_ref: ArtifactRef = Field(
        description="Artifact holding the uncertain Markdown fragment."
    )
    issue_codes: list[str] = Field(
        description="Deterministic reasons that justify model assistance."
    )
    locators: list[SourceLocator] = Field(
        description="Original source locations for audit."
    )
    instruction: str = Field(
        description="Narrow repair instruction; summarization is forbidden."
    )


class StructureRepairResult(BaseModel):
    """保留全部内容的结构修复结果。"""

    markdown: str = Field(
        description="Repaired Markdown without summarization or omitted content."
    )
    model: str = Field(description="Model identifier and revision.")
    notes: str = Field(
        default="", description="Short explanation of structural changes."
    )


class StructureRefiner(Protocol):
    """低置信结构修复的可替换 LLM 协议。"""

    async def repair(self, request: StructureRepairRequest) -> StructureRepairResult:
        """只修复请求指出的结构问题，不能重写正常内容。"""

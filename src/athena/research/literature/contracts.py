"""Data contracts shared only across literature conversion boundaries."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef

TexSourceFormat = Literal["auto", "tar", "tar.gz", "zip", "gzip", "plain"]
VisualPolicy = Literal["required", "best_effort"]


class ChunkingConfig(BaseModel):
    """Deterministic size policy for structure-preserving RAG chunks."""

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


class ProcessingDiagnostic(BaseModel):
    """Stable conversion-quality fact shared by source and Markdown stages."""

    level: Literal["info", "warning", "error"] = Field(
        description="Diagnostic severity."
    )
    code: str = Field(description="Stable machine-readable diagnostic code.")
    message: str = Field(description="Human-readable diagnostic message.")
    locator: Any | None = Field(
        default=None,
        description="Package-owned source locator associated with the diagnostic.",
    )
    evidence_refs: list[ArtifactRef] = Field(
        default_factory=list,
        description="Artifacts that substantiate the diagnostic or model-assisted change.",
    )

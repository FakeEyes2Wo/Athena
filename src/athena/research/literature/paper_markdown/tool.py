"""Athena tool boundary for RAG-oriented paper conversion."""

import asyncio

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.literature.paper_markdown.interfaces import (
    StructureRefiner,
    VisualInterpreter,
)
from athena.research.literature.paper_markdown.processor import (
    DEFAULT_VISUAL_CONCURRENCY,
    PaperProcessor,
)
from athena.research.literature.paper_markdown.schemas import PaperConversionRequest
from athena.core.contracts import ArtifactStore


class PaperMarkdownTool(BaseTool):
    """Convert an upstream TeX or PDF artifact into persisted RAG paper content.

    The tool returns only the top-level result reference. The complete Markdown,
    chunks, visuals, diagnostics, and model outputs remain in ``ArtifactStore``.
    """

    spec = ToolSpec(
        name="paper_markdown",
        description=(
            "Convert an upstream-provided TeX source package or PDF artifact into "
            "persisted Markdown and RAG retrieval units."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Artifact reference containing a PaperConversionRequest JSON object."
                    ),
                }
            },
            "required": ["request_ref"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    def __init__(
        self,
        artifacts: ArtifactStore,
        visual_interpreter: VisualInterpreter | None,
        structure_refiner: StructureRefiner | None = None,
        visual_concurrency: int = DEFAULT_VISUAL_CONCURRENCY,
        ghostscript: str | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.processor = PaperProcessor(
            artifacts,
            visual_interpreter,
            structure_refiner,
            visual_concurrency=visual_concurrency,
            ghostscript=ghostscript,
        )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """Process one persisted conversion request and return its result reference."""
        request_ref = input.get("request_ref")
        if not isinstance(request_ref, str) or not request_ref.strip():
            raise ValueError("request_ref must be a non-empty artifact reference.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        payload = await self.artifacts.get_text(request_ref)
        request = PaperConversionRequest.model_validate_json(payload)
        content = await self.processor.process(request)
        result_ref = await self.artifacts.put_text(content.model_dump_json())
        return ToolResult(
            data={"paper_content_ref": result_ref},
            artifacts=[result_ref],
        )

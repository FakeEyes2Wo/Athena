"""paper_source 的 Athena 工具边界。

产出刻意做成"可直接调用的下游输入"：每篇论文都落盘一个 ``PaperConversionRequest``，工具
返回其引用列表，Agent 只需把引用逐个转交 ``paper_markdown``，无需自己拼装 tex/pdf 引用与
格式字段。
"""

import asyncio

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.literature.paper_source.fetcher import (
    LocatorCache,
    PaperSourceFetcher,
)
from athena.research.literature.paper_source.http import HostRateLimiter
from athena.research.literature.paper_source.schemas import PaperSourceRequest
from athena.core.contracts import ArtifactStore


class PaperFetchTool(BaseTool):
    """把上游论文标识符解析成版本固定的 TeX/PDF artifact 与转换请求。

    只返回顶层引用与计数；逐篇记录、诊断与原始字节都留在 ``ArtifactStore`` 里，避免超出
    工具结果的字符预算。
    """

    spec = ToolSpec(
        name="paper_fetch",
        description=(
            "Resolve upstream paper identifiers into version-pinned arXiv TeX source or "
            "PDF artifacts and persisted paper_markdown conversion requests."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Artifact reference containing a PaperSourceRequest JSON object."
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
        http: HostRateLimiter | None = None,
        cache: LocatorCache | None = None,
        contact_email: str | None = None,
        openalex_api_key: str | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.fetcher = PaperSourceFetcher(
            artifacts,
            http=http,
            cache=cache,
            contact_email=contact_email,
            openalex_api_key=openalex_api_key,
        )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """执行一次批量取源，返回结果引用与可继续调用的转换请求引用。"""
        request_ref = input.get("request_ref")
        if not isinstance(request_ref, str) or not request_ref.strip():
            raise ValueError("request_ref must be a non-empty artifact reference.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError

        payload = await self.artifacts.get_text(request_ref)
        request = PaperSourceRequest.model_validate_json(payload)
        result = await self.fetcher.fetch(request, ctx.cancel)
        result_ref = await self.artifacts.put_text(result.model_dump_json())
        conversion_refs = result.conversion_request_refs()
        return ToolResult(
            data={
                "result_ref": result_ref,
                "conversion_request_refs": conversion_refs,
                "fetched": result.stats.fetched,
                "skipped": result.stats.skipped,
                "failed": result.stats.failed,
                "http_requests": result.stats.http_requests,
            },
            artifacts=[result_ref, *conversion_refs],
        )

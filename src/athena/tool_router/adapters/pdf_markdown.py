"""PDF -> 可寻址 Markdown 的工具适配器。

与 codex、qoder 等适配器一样，本适配器只按单一 capability 执行受控请求，并以
artifact 交接请求与结果，不自行推进实验、选择指标或绕过 Supervisor：

    请求 artifact(JSON: MarkdownifyRequest)         结果 artifact(JSON: PaperMarkdown)
        pdf_ref ──► get_bytes ──► markitdown 逐页转换 ──► put_text ──► 返回结果引用

markitdown 为 CPU/阻塞型同步计算，故在工作线程中执行（asyncio.to_thread），避免阻塞
事件循环。工具权限、并发优先级、重试与审批仍由 Scheduler、ThreadManager 与 Supervisor
协作管理；本适配器只负责"取 PDF -> 转 Markdown -> 存结果"。

在 ToolRouter 中注册与调用：
    adapter = PdfMarkdownAdapter(artifact_store)
    router.register(PdfMarkdownAdapter.CAPABILITY, adapter)
    result_ref = await router.invoke(PdfMarkdownAdapter.CAPABILITY, request_ref)
"""

import asyncio

from pydantic import BaseModel, Field

from athena.core.schemas import ArtifactRef
from athena.research.pdf_markdown.converter import pdf_bytes_to_markdown
from athena.storage.artifact_store import ArtifactStore


# ====== 导入的包 ======
# 本模块无模块级常量。
# ======


class MarkdownifyRequest(BaseModel):
    """PDF -> 可寻址 Markdown 工具的请求（以 artifact 交接）。"""

    pdf_ref: ArtifactRef = Field(description="Artifact reference to the source PDF bytes.")


class PdfMarkdownAdapter:
    """借助 markitdown 把论文 PDF 转成可寻址 Markdown 的工具适配器。

    输入：指向请求 artifact 的 ``ArtifactRef``（其内容为 ``MarkdownifyRequest`` 的 JSON）。
    输出：指向结果 artifact 的 ``ArtifactRef``（其内容为 ``PaperMarkdown`` 的 JSON）。
    """

    # 工具在 ToolRouter 中注册所用的 capability 名称（对应 design.md 的 Markdownify 阶段）。
    CAPABILITY = "markdownify"

    def __init__(self, artifacts: ArtifactStore) -> None:
        # 大对象交接所依赖的 artifact 存储。
        self._artifacts = artifacts

    async def invoke(self, request_ref: ArtifactRef) -> ArtifactRef:
        """执行一次 PDF->Markdown 转换，返回 PaperMarkdown artifact 的引用。

        示例：
            result_ref = await adapter.invoke(request_ref)
            paper = PaperMarkdown.model_validate_json(await store.get_text(result_ref))
        """
        request = MarkdownifyRequest.model_validate_json(await self._artifacts.get_text(request_ref))
        pdf_bytes = await self._artifacts.get_bytes(request.pdf_ref)
        paper = await asyncio.to_thread(pdf_bytes_to_markdown, pdf_bytes)
        return await self._artifacts.put_text(paper.model_dump_json())

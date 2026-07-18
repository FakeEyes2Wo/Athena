"""``PdfMarkdownAdapter`` 作为 Athena 工具（经 ToolRouter）的端到端测试。

复用 test_pdf_markdown 里的 ``make_pdf`` 现场生成最小 PDF，验证：把 PDF 与请求写入
ArtifactStore、经 ToolRouter 按 capability 调用、再从结果 artifact 读回 PaperMarkdown。
"""

import os
import tempfile
import unittest

from athena.research.pdf_markdown import PaperMarkdown
from athena.research.pdf_markdown.converter import pdf_bytes_to_markdown
from athena.storage import LocalArtifactStore
from athena.tool_router import ToolRouter
from athena.tool_router.adapters.pdf_markdown import MarkdownifyRequest, PdfMarkdownAdapter

from unit.test_pdf_markdown import SAMPLE_PAGES, make_pdf


# ====== 导入的包 ======


class PdfBytesConverterTest(unittest.TestCase):
    """字节入口的转换测试。"""

    def test_pdf_bytes_to_markdown(self) -> None:
        """pdf_bytes_to_markdown 直接吃字节，产出与文件入口一致的结构。"""
        result = pdf_bytes_to_markdown(make_pdf(SAMPLE_PAGES))
        self.assertEqual(2, result.page_count)
        self.assertTrue(any(f.anchor == "fig-1" for f in result.figures))


class AdapterThroughRouterTest(unittest.IsolatedAsyncioTestCase):
    """把适配器注册进 ToolRouter，按 capability 调用的完整链路测试。"""

    async def asyncSetUp(self) -> None:
        """建店、注册工具、并把样例 PDF 与请求写成 artifact。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalArtifactStore(os.path.join(self.tmp.name, "artifacts"))
        self.router = ToolRouter()
        self.router.register(PdfMarkdownAdapter.CAPABILITY, PdfMarkdownAdapter(self.store))
        self.pdf_ref = await self.store.put_bytes(make_pdf(SAMPLE_PAGES))
        self.request_ref = await self.store.put_text(
            MarkdownifyRequest(pdf_ref=self.pdf_ref).model_dump_json()
        )

    async def asyncTearDown(self) -> None:
        """清理临时目录。"""
        self.tmp.cleanup()

    async def test_capability_registered(self) -> None:
        """工具以 'markdownify' 能力出现在路由表中。"""
        self.assertIn("markdownify", self.router.capabilities)

    async def test_invoke_returns_paper_markdown_artifact(self) -> None:
        """经 router.invoke 调用后，结果 artifact 是可解析的 PaperMarkdown，含页码/图表。"""
        result_ref = await self.router.invoke(PdfMarkdownAdapter.CAPABILITY, self.request_ref)
        paper = PaperMarkdown.model_validate_json(await self.store.get_text(result_ref))
        self.assertEqual(2, paper.page_count)
        self.assertTrue(paper.converter.startswith("markitdown-"))
        self.assertTrue(any(h.title == "2 Methods" and h.page_number == 2 for h in paper.headings))
        self.assertTrue(any(f.anchor == "fig-1" and f.page_number == 2 for f in paper.figures))

    async def test_result_is_content_addressed(self) -> None:
        """同一请求两次调用返回同一结果引用（内容寻址去重）。"""
        first = await self.router.invoke(PdfMarkdownAdapter.CAPABILITY, self.request_ref)
        second = await self.router.invoke(PdfMarkdownAdapter.CAPABILITY, self.request_ref)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

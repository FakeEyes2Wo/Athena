"""``PdfMarkdownAdapter`` 工具与 ``PaperContent`` 落库/查询的端到端测试。

复用 test_pdf_markdown 里的 ``make_pdf`` 现场生成最小 PDF，验证：
- 转换 -> 落库为 ``PaperContent``（整篇 markdown 单独成 artifact，索引内联可查询）；
- 经 ToolRouter 按 capability 调用工具，从结果 artifact 读回并查询索引。
"""

import os
import tempfile
import unittest

from athena.research.pdf_markdown import PaperContent
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


class PaperContentPersistTest(unittest.IsolatedAsyncioTestCase):
    """PaperMarkdown -> PaperContent 落库与持久化后查询的测试。"""

    async def asyncSetUp(self) -> None:
        """转换样例 PDF 并落库为 PaperContent。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalArtifactStore(os.path.join(self.tmp.name, "artifacts"))
        self.paper = pdf_bytes_to_markdown(make_pdf(SAMPLE_PAGES))
        self.content = await self.paper.persist(self.store)

    async def asyncTearDown(self) -> None:
        """清理临时目录。"""
        self.tmp.cleanup()

    async def test_full_markdown_stored_once_and_loadable(self) -> None:
        """整篇 markdown 存为一份内容寻址 artifact，可原样读回。"""
        self.assertTrue(self.content.markdown_ref.startswith("sha256:"))
        self.assertEqual(self.paper.markdown, await self.content.load_markdown(self.store))

    async def test_sections_reconstructed_without_separate_storage(self) -> None:
        """逐章节文本按需从整篇 markdown 切片重建，与内存态一致（不单独落盘）。"""
        self.assertEqual(self.paper.sections, await self.content.load_sections(self.store))

    async def test_index_preserved_and_queryable(self) -> None:
        """页码/章节/图表索引在持久化后完整保留且可直接查询。"""
        self.assertEqual(self.paper.headings, self.content.headings)
        self.assertEqual(self.paper.figures, self.content.figures)
        self.assertEqual(self.paper.pages, self.content.pages)
        # 查询：按标签定位图题注，按偏移反查页码。
        figure = self.content.find_float("Figure 1")
        self.assertEqual("fig-1", figure.anchor)
        self.assertEqual(figure.page_number, self.content.page_of(figure.char_offset))
        # 章节索引可在不加载正文的情况下枚举。
        paths = [path for path, _, _ in self.content.section_spans()]
        self.assertIn("2 Methods", paths)


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

    async def test_invoke_returns_queryable_paper_content(self) -> None:
        """经 router.invoke 后，结果 artifact 是可查询索引、可加载整篇 markdown 的 PaperContent。"""
        result_ref = await self.router.invoke(PdfMarkdownAdapter.CAPABILITY, self.request_ref)
        content = PaperContent.model_validate_json(await self.store.get_text(result_ref))
        self.assertEqual(2, content.page_count)
        self.assertTrue(content.converter.startswith("markitdown-"))
        # 索引可直接查询。
        self.assertTrue(any(h.title == "2 Methods" and h.page_number == 2 for h in content.headings))
        self.assertTrue(any(f.anchor == "fig-1" and f.page_number == 2 for f in content.figures))
        # 整篇 markdown 经引用可加载。
        markdown = await content.load_markdown(self.store)
        self.assertIn("# 2 Methods", markdown)

    async def test_result_is_content_addressed(self) -> None:
        """同一请求两次调用返回同一结果引用（内容寻址去重）。"""
        first = await self.router.invoke(PdfMarkdownAdapter.CAPABILITY, self.request_ref)
        second = await self.router.invoke(PdfMarkdownAdapter.CAPABILITY, self.request_ref)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

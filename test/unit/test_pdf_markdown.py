"""``athena.research.pdf_markdown`` 的单元测试。

覆盖两类函数：
- 纯函数（slugify / classify_line / push_heading / unique_anchor / build_sections /
  find_cross_references / page_of_offset / build_paper_markdown）用合成输入直接验证；
- 依赖真实 PDF 的函数（fingerprint_pdf / convert_pdf_pages / pdf_to_markdown）用本文件
  内的 ``make_pdf`` 现场生成一个最小的、文本已知的多页 PDF，保持测试自足、不依赖外部文件。
"""

import io
import os
import tempfile
import unittest

from pypdf import PdfReader

from athena.research.pdf_markdown import pdf_to_markdown
from athena.research.pdf_markdown.converter import (
    build_paper_markdown,
    build_sections,
    classify_line,
    convert_pdf_pages,
    find_cross_references,
    fingerprint_pdf,
    push_heading,
    slugify,
    unique_anchor,
)
from athena.research.pdf_markdown.schemas import Heading, PageSpan, page_of_offset


# ====== 导入的包 ======


def make_pdf(pages: list[list[str]]) -> bytes:
    """生成一个最小合法 PDF，每页按行打印给定的 ASCII 文本。

    输入：pages 为"页 -> 该页文本行列表"；输出：可被 pypdf/markitdown 读取的 PDF 字节。
    仅供测试使用：Helvetica 12pt，行距 14pt，正文从左上角起排。
    """
    objects: list[bytes] = []

    def content_stream(lines: list[str]) -> bytes:
        """把一页的文本行编译成 PDF 内容流字节。"""
        body = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
        for index, line in enumerate(lines):
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            if index > 0:
                body.append("T*")
            body.append(f"({escaped}) Tj")
        body.append("ET")
        return "\n".join(body).encode("latin-1")

    page_count = len(pages)
    page_ids = [3 + i for i in range(page_count)]
    content_ids = [3 + page_count + i for i in range(page_count)]
    font_id = 3 + 2 * page_count

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    for index in range(page_count):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>".encode()
        )
    for index in range(page_count):
        stream = content_stream(pages[index])
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    total = len(objects) + 1
    out.write(f"xref\n0 {total}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode())
    return out.getvalue()


# 测试共用的两页样例：第 1 页有带编号标题与对图 1 的引用，第 2 页有图 1 题注与第二个标题。
# make_pdf 需要"页 -> 文本行列表"；build_paper_markdown 需要"页 -> 整页字符串"，故派生后者。
SAMPLE_PAGES = [
    ["1 Introduction", "This is body text.", "See Figure 1 for the architecture."],
    ["Figure 1: The overall architecture.", "2 Methods", "The method body text."],
]
SAMPLE_PAGE_TEXTS = ["\n".join(lines) for lines in SAMPLE_PAGES]

# ======


class PureFunctionTest(unittest.TestCase):
    """不依赖真实 PDF 的纯函数测试。"""

    def test_slugify(self) -> None:
        """slugify 归一化为小写连字符 slug。"""
        self.assertEqual("4-1-overview", slugify("4.1 Overview"))
        self.assertEqual("x", slugify("!!!"))

    def test_classify_line_heading_and_float(self) -> None:
        """classify_line 区分编号标题、关键词标题、图题注、表题注与普通行。"""
        self.assertEqual("heading", classify_line("4.1 Overview").kind)
        self.assertEqual(2, classify_line("4.1 Overview").level)
        self.assertEqual("heading", classify_line("Abstract").kind)
        self.assertEqual("figure", classify_line("Figure 1: Arch").kind)
        self.assertEqual("table", classify_line("Table 2. Stats").kind)
        self.assertIsNone(classify_line("just some prose text"))
        # 关键词以 startswith 会误命中，归一化等值匹配则不会。
        self.assertIsNone(classify_line("References retrieval qualities of engines"))

    def test_classify_line_rejects_prose_and_bare_refs(self) -> None:
        """以数字开头的正文句与无说明文字的图表引用不应被误判。"""
        # 正文句含句内 ". " 且断行连字符结尾，不是编号标题。
        self.assertIsNone(classify_line("50 instances. On average, each query is associ-"))
        # "Table14." 是正文引用（无题注说明文字），不是题注。
        self.assertIsNone(classify_line("Table14."))
        # 但带说明文字的题注仍应识别（即便编号后无空格）。
        self.assertEqual("table", classify_line("Table4: Results on the test set").kind)

    def test_push_heading_builds_ancestor_path(self) -> None:
        """push_heading 维护层级栈并给出祖先链路径。"""
        stack: list[tuple[int, str]] = []
        self.assertEqual("4 Methodology", push_heading(stack, 1, "4 Methodology"))
        self.assertEqual("4 Methodology / 4.1 Overview", push_heading(stack, 2, "4.1 Overview"))
        # 回到顶层时应弹出旧的子节点。
        self.assertEqual("5 Experiments", push_heading(stack, 1, "5 Experiments"))

    def test_unique_anchor_dedupes(self) -> None:
        """unique_anchor 对重复基名追加序号并登记。"""
        used: set[str] = set()
        self.assertEqual("fig-1", unique_anchor("fig-1", used))
        self.assertEqual("fig-1-2", unique_anchor("fig-1", used))
        self.assertIn("fig-1-2", used)

    def test_page_of_offset(self) -> None:
        """page_of_offset 按区间定位页码，越界取最近合法页。"""
        pages = [PageSpan(page_number=1, char_start=0, char_end=10),
                 PageSpan(page_number=2, char_start=10, char_end=20)]
        self.assertEqual(1, page_of_offset(pages, 3))
        self.assertEqual(2, page_of_offset(pages, 15))
        self.assertEqual(2, page_of_offset(pages, 999))

    def test_build_sections_slices_by_heading(self) -> None:
        """build_sections 以标题偏移切分，并保留 preamble。"""
        markdown = "title page\n## 1 Intro  <!-- sec-1-intro p:1 -->\nbody"
        offset = markdown.index("## 1 Intro")
        headings = [Heading(level=1, title="1 Intro", path="1 Intro",
                            page_number=1, char_offset=offset, anchor="sec-1-intro")]
        sections = build_sections(markdown, headings)
        self.assertEqual("title page", sections["(preamble)"])
        self.assertIn("body", sections["1 Intro"])

    def test_find_cross_references_resolves_anchor(self) -> None:
        """find_cross_references 命中正文引用并解析到题注锚点，且排除题注本身。"""
        result = build_paper_markdown(SAMPLE_PAGE_TEXTS, "fp", "markitdown-test")
        refs = result.cross_references
        self.assertTrue(any(r.target_anchor == "fig-1" and r.page_number == 1 for r in refs))
        # 题注所在偏移不应被算作一次引用。
        caption_offset = result.figures[0].char_offset
        self.assertFalse(any(r.char_offset == caption_offset for r in refs))


class AssemblerTest(unittest.TestCase):
    """build_paper_markdown 组装与索引一致性测试。"""

    def setUp(self) -> None:
        """用合成两页文本组装一次，供各断言复用。"""
        self.result = build_paper_markdown(SAMPLE_PAGE_TEXTS, "fp", "markitdown-test")

    def test_page_anchors_and_count(self) -> None:
        """整篇 markdown 含每页页码锚点，页数正确。"""
        self.assertEqual(2, self.result.page_count)
        self.assertIn("<!-- page:1 -->", self.result.markdown)
        self.assertIn("<!-- page:2 -->", self.result.markdown)

    def test_headings_promoted_and_attributed(self) -> None:
        """标题被提升为 markdown 标题并带正确页码。"""
        titles = {h.title: h.page_number for h in self.result.headings}
        self.assertEqual(1, titles["1 Introduction"])
        self.assertEqual(2, titles["2 Methods"])
        # "2 Methods" 为单段编号，属顶层标题，提升为单个 "#"。
        self.assertIn("# 2 Methods", self.result.markdown)

    def test_figure_registered_with_page(self) -> None:
        """图题注登记为 FloatRef，页码与锚点正确。"""
        self.assertEqual(1, len(self.result.figures))
        figure = self.result.figures[0]
        self.assertEqual("figure", figure.kind)
        self.assertEqual("Figure 1", figure.label)
        self.assertEqual(2, figure.page_number)
        self.assertEqual("fig-1", figure.anchor)

    def test_offsets_map_back_to_pages(self) -> None:
        """每个标题/题注的字符偏移经 page_of 反查得到其登记页码。"""
        for head in self.result.headings:
            self.assertEqual(head.page_number, self.result.page_of(head.char_offset))
        for figure in self.result.figures:
            self.assertEqual(figure.page_number, self.result.page_of(figure.char_offset))

    def test_pages_cover_markdown_contiguously(self) -> None:
        """页码区间首尾相接且覆盖整篇 markdown。"""
        spans = self.result.pages
        self.assertEqual(0, spans[0].char_start)
        self.assertEqual(len(self.result.markdown) + 1, spans[-1].char_end)
        for earlier, later in zip(spans, spans[1:]):
            self.assertEqual(earlier.char_end, later.char_start)


class PdfBackedTest(unittest.TestCase):
    """依赖真实 PDF 的函数测试，使用现场生成的最小 PDF。"""

    def setUp(self) -> None:
        """把样例 PDF 写入临时目录，供文件级函数读取。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf_path = os.path.join(self.tmp.name, "sample.pdf")
        with open(self.pdf_path, "wb") as handle:
            handle.write(make_pdf(SAMPLE_PAGES))

    def tearDown(self) -> None:
        """清理临时目录。"""
        self.tmp.cleanup()

    def test_fingerprint_is_deterministic_and_content_bound(self) -> None:
        """相同内容指纹一致，内容不同则指纹不同。"""
        first = fingerprint_pdf(self.pdf_path)
        self.assertEqual(64, len(first))
        self.assertEqual(first, fingerprint_pdf(self.pdf_path))
        other = os.path.join(self.tmp.name, "other.pdf")
        with open(other, "wb") as handle:
            handle.write(make_pdf([["totally different content"]]))
        self.assertNotEqual(first, fingerprint_pdf(other))

    def test_convert_pdf_pages_preserves_page_boundaries(self) -> None:
        """convert_pdf_pages 返回逐页文本，页码与文本对应。"""
        texts = convert_pdf_pages(self.pdf_path)
        self.assertEqual(2, len(texts))
        self.assertIn("Introduction", texts[0])
        self.assertIn("Methods", texts[1])

    def test_pdf_to_markdown_end_to_end(self) -> None:
        """端到端：得到含页码/章节/图表引用的可寻址 markdown。"""
        result = pdf_to_markdown(self.pdf_path)
        self.assertTrue(result.converter.startswith("markitdown-"))
        self.assertEqual(2, result.page_count)
        self.assertTrue(any(h.title == "2 Methods" and h.page_number == 2 for h in result.headings))
        self.assertTrue(any(f.anchor == "fig-1" and f.page_number == 2 for f in result.figures))

    def test_cache_round_trip(self) -> None:
        """带 cache_dir 时按指纹缓存，二次调用从缓存读回等价结果。"""
        cache_dir = os.path.join(self.tmp.name, "cache")
        first = pdf_to_markdown(self.pdf_path, cache_dir=cache_dir)
        cache_file = os.path.join(cache_dir, f"{first.fingerprint}.json")
        self.assertTrue(os.path.exists(cache_file))
        second = pdf_to_markdown(self.pdf_path, cache_dir=cache_dir)
        self.assertEqual(first.model_dump(), second.model_dump())


if __name__ == "__main__":
    unittest.main()

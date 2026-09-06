"""图片格式判定与预览渲染 —— 决定一张图能不能被模型读到。

真机基线（44 篇论文、1347 张图）：png 923、pdf 189、无声明 145、eps 59、jpeg 27。
唯一渲染不出来的是 EPS，而它恰恰是 2015 年前 arXiv 论文的默认插图格式。
"""

import shutil
import unittest

import fitz

from athena.research.literature.paper_markdown.visuals import (
    POSTSCRIPT_TYPES,
    VISION_READABLE,
    render_preview,
    sniff_media_type,
)

EPS = b"""%!PS-Adobe-3.0 EPSF-3.0
%%BoundingBox: 0 0 100 60
%%EndComments
newpath 10 10 moveto 80 40 lineto 4 setlinewidth stroke
showpage
%%EOF
"""

SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'


def dos_eps(payload: bytes) -> bytes:
    """按 EPSF 二进制封装格式包一层。

    头部 30 字节：4 字节魔数，之后是 PostScript 段的偏移与长度（小端），再往后是
    WMF/TIFF 预览段的偏移与长度。偏移和长度必须写对——填零的话 Ghostscript 会认为
    PostScript 段是空的，安静地渲染出零页。
    """
    header = bytearray(30)
    header[0:4] = b"\xc5\xd0\xd3\xc6"
    header[4:8] = (30).to_bytes(4, "little")
    header[8:12] = len(payload).to_bytes(4, "little")
    return bytes(header) + payload


DOS_EPS = dos_eps(EPS)


def bitmap(fmt: str) -> bytes:
    """用 MuPDF 造一张最小位图，避免测试依赖外部素材。"""
    return fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False).tobytes(fmt)


def ghostscript() -> str | None:
    for name in ("gs", "gswin64c", "gswin32c", "mgs", "rungs"):
        found = shutil.which(name)
        if found:
            return found
    return None


class SniffTest(unittest.TestCase):
    """格式一律按魔数判定 —— TeX 里的扩展名只是作者的写法，不是事实。"""

    def test_magic_beats_a_wrong_declaration(self):
        self.assertEqual("image/png", sniff_media_type(bitmap("png"), "image/jpeg"))

    def test_postscript_is_recognized(self):
        self.assertEqual("application/postscript", sniff_media_type(EPS, None))

    def test_dos_eps_binary_header_is_recognized(self):
        """EPSF 的二进制包装头在 PostScript 前面，认不出来就会当成未知格式丢掉。"""
        self.assertEqual("application/postscript", sniff_media_type(DOS_EPS, None))

    def test_common_raster_formats_are_recognized(self):
        for fmt, expected in (("png", "image/png"), ("jpg", "image/jpeg")):
            with self.subTest(format=fmt):
                self.assertEqual(expected, sniff_media_type(bitmap(fmt), None))

    def test_pdf_and_svg_are_recognized(self):
        self.assertEqual("application/pdf", sniff_media_type(b"%PDF-1.7\n", None))
        self.assertEqual("image/svg+xml", sniff_media_type(SVG, None))

    def test_an_unknown_payload_falls_back_to_the_declaration(self):
        self.assertEqual(
            "image/tiff", sniff_media_type(b"\x00\x01\x02\x03", "image/tiff")
        )
        self.assertEqual("", sniff_media_type(b"\x00\x01\x02\x03", None))


class PreviewTest(unittest.TestCase):
    def test_a_raster_asset_becomes_a_png(self):
        result = render_preview(bitmap("jpg"), "image/jpeg")

        self.assertIsNotNone(result.png)
        self.assertEqual("image/jpeg", result.media_type)
        self.assertEqual("", result.reason)
        self.assertEqual(b"\x89PNG", result.png[:4])

    def test_an_empty_asset_is_reported_not_crashed(self):
        self.assertEqual("empty_asset", render_preview(b"", "image/png").reason)

    def test_postscript_without_ghostscript_says_so(self):
        """区分"缺工具"和"图坏了"：前者补上依赖就能全部救回，后者只能认了。"""
        result = render_preview(EPS, "application/postscript", ghostscript=None)

        self.assertIsNone(result.png)
        self.assertEqual("ghostscript_missing", result.reason)
        self.assertIn(result.media_type, POSTSCRIPT_TYPES)

    def test_a_corrupt_asset_is_distinguished_from_a_missing_converter(self):
        result = render_preview(b"\x89PNG\r\n\x1a\ngarbage", "image/png")

        self.assertIsNone(result.png)
        self.assertEqual("unsupported_format", result.reason)

    @unittest.skipUnless(ghostscript(), "no Ghostscript on PATH")
    def test_postscript_is_rendered_when_ghostscript_exists(self):
        result = render_preview(
            EPS, "application/postscript", ghostscript=ghostscript()
        )

        self.assertEqual("", result.reason)
        self.assertEqual(b"\x89PNG", result.png[:4])
        pixmap = fitz.Pixmap(result.png)
        self.assertGreater(pixmap.width, 0)

    @unittest.skipUnless(ghostscript(), "no Ghostscript on PATH")
    def test_the_dos_eps_wrapper_is_rendered_too(self):
        result = render_preview(DOS_EPS, None, ghostscript=ghostscript())

        self.assertEqual("", result.reason)
        self.assertEqual(b"\x89PNG", result.png[:4])


class VisionReadableTest(unittest.TestCase):
    def test_postscript_is_never_inlined(self):
        """真机命中：59 张 EPS 被原样 base64 发给模型，全部被拒，白花 59 次调用。"""
        for media_type in POSTSCRIPT_TYPES:
            with self.subTest(media_type=media_type):
                self.assertNotIn(media_type, VISION_READABLE)

    def test_pdf_is_never_inlined_either(self):
        self.assertNotIn("application/pdf", VISION_READABLE)
        self.assertNotIn("image/svg+xml", VISION_READABLE)

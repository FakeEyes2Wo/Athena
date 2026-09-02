"""把 TeX 原始图片或 PDF 区域规范成 VLM 可读取的 PNG 预览。

格式判定一律按魔数，不信上游声明的 media type：TeX 里 ``\\includegraphics`` 的扩展名
只是作者的写法，实测同一批论文里既有扩展名与内容不符的，也有根本没有声明类型的。

渲染分两条路。MuPDF 覆盖 PDF、SVG 与全部常见位图；PostScript 家族（EPS/PS）它一概打
不开——实测 ``fitz.open`` 对 ``filetype`` 取 ps/eps/pdf/None 全部抛
``FileDataError``，``fitz.Pixmap`` 报 ``unknown image file format``——因此单独走
Ghostscript。PostScript 是图灵完备语言，没有纯 Python 的替代实现。
"""

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from athena.research.literature.paper_markdown.document import ParsedVisual
from athena.research.literature.paper_markdown.interfaces import VisualInterpretation
from athena.research.literature.paper_markdown.schemas import PaperVisual

FITZ_FILE_TYPES = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/svg+xml": "svg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/webp": "webp",
    "image/jp2": "jpx",
    "image/x-portable-anymap": "pnm",
    "image/vnd.adobe.photoshop": "psd",
}

POSTSCRIPT_TYPES = frozenset({"application/postscript", "image/x-eps"})

MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"%!PS", "application/postscript"),
    # DOS EPS：二进制包装头后面才是真正的 PostScript，Ghostscript 能直接吃
    (b"\xc5\xd0\xd3\xc6", "application/postscript"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x00\x00\x00\x0cjP  ", "image/jp2"),
    (b"\xff\x4f\xff\x51", "image/jp2"),
    (b"8BPS", "image/vnd.adobe.photoshop"),
)

PNM_MAGICS = frozenset({b"P1", b"P2", b"P3", b"P4", b"P5", b"P6", b"P7"})
SVG_MARKERS = (b"<svg", b"<!doctype svg")
XML_PROLOGUE = b"<?xml"
SNIFF_WINDOW = 1024
DEFAULT_PREVIEW_DPI = 180
GHOSTSCRIPT_TIMEOUT = 60.0

VISION_READABLE = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp"}
)
"""可以直接内联给多模态模型的格式。

其余格式即使 MuPDF 打得开，也必须先转成 PNG 再送：真机上 59 张 EPS 被原样 base64
成 ``data:application/postscript`` 发出去，模型全部拒收——白花 59 次调用，还把失败
计数抬高到看不出真正的模型故障。
"""


@dataclass(slots=True)
class PreviewResult:
    """预览渲染的结果；``reason`` 非空表示没渲染出来，且说明了原因。"""

    png: bytes | None = None
    media_type: str = ""
    reason: str = ""


def sniff_media_type(data: bytes, declared: str | None = None) -> str:
    """按魔数判定真实格式；认不出时才退回上游声明的类型。"""
    head = data[:SNIFF_WINDOW]
    for signature, media_type in MAGIC_SIGNATURES:
        if head.startswith(signature):
            return media_type
    if head[:2] in PNM_MAGICS:
        return "image/x-portable-anymap"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    lowered = head.lower()
    if lowered.startswith(XML_PROLOGUE) or lowered.lstrip().startswith(b"<"):
        if any(marker in lowered for marker in SVG_MARKERS):
            return "image/svg+xml"
    return (declared or "").strip().lower()


def render_with_fitz(data: bytes, media_type: str, dpi: int) -> bytes | None:
    """用 MuPDF 渲染首页/首帧为 PNG；打不开或格式不受支持时返回 ``None``。"""
    file_type = FITZ_FILE_TYPES.get(media_type)
    try:
        with fitz.open(stream=data, filetype=file_type) as document:
            if len(document) == 0:
                return None
            pixmap = document[0].get_pixmap(
                matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False
            )
            return pixmap.tobytes("png")
    except Exception:
        # 文档路径打不开（多数位图不是"文档"）→ 退到位图解码器再试一次
        pass
    try:
        return fitz.Pixmap(data).tobytes("png")
    except Exception:
        # 内容损坏或这个 MuPDF 版本不认识该格式 → 交给调用方记诊断
        return None


def render_postscript(data: bytes, executable: str, dpi: int) -> bytes | None:
    """用 Ghostscript 把 EPS/PS 渲染成 PNG。

    ``-dEPSCrop`` 按 BoundingBox 裁剪，否则 EPS 会被摊到整页纸上，图缩在角落里，
    模型读到的几乎全是空白。输出走临时文件而不是 stdout：实测 ``gsnd`` 往 stdout
    写会得到 0 字节。
    """
    with tempfile.TemporaryDirectory(prefix="athena_eps_") as directory:
        source = Path(directory) / "source.eps"
        target = Path(directory) / "preview.png"
        source.write_bytes(data)
        arguments = [
            executable,
            "-q",
            "-dNOPAUSE",
            "-dBATCH",
            "-dSAFER",
            "-dEPSCrop",
            "-sDEVICE=png16m",
            f"-r{dpi}",
            f"-sOutputFile={target}",
            str(source),
        ]
        try:
            done = subprocess.run(
                arguments, capture_output=True, timeout=GHOSTSCRIPT_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            # 可执行文件不存在、无权限或渲染超时 → 当作渲染不出来
            return None
        if done.returncode != 0 or not target.exists():
            return None
        payload = target.read_bytes()
        return payload or None


def fallback_interpretation(visual: ParsedVisual) -> VisualInterpretation:
    """在 best-effort 模式中仅返回已有的确定性证据。"""
    evidence = visual.structured_text or visual.caption
    return VisualInterpretation(
        summary=evidence
        or "Visual content was preserved but could not be interpreted.",
        searchable_text=evidence
        or f"Uninterpreted {visual.kind} at {visual.visual_id}.",
        structured_data={"interpretation_status": "unavailable"},
        model="deterministic-evidence-only",
        confidence=None,
    )


def visual_markdown(
    visual: ParsedVisual,
    stored: PaperVisual,
    interpretation: VisualInterpretation,
) -> str:
    """生成插回正文的视觉引用和新增解释。"""
    explanation = interpretation.searchable_text.strip()
    if visual.kind == "page":
        return explanation
    display_ref = stored.preview_ref or stored.asset_ref
    lines = [f"[Visual source](athena-artifact://{display_ref})"] if display_ref else []
    existing_evidence = {
        value.strip()
        for value in (visual.caption, visual.structured_text or "")
        if value.strip()
    }
    if explanation and explanation not in existing_evidence:
        lines.append(f"> **Visual interpretation:** {explanation}")
    return "\n\n".join(lines)


def render_preview(
    data: bytes,
    media_type: str | None,
    dpi: int = DEFAULT_PREVIEW_DPI,
    ghostscript: str | None = None,
) -> PreviewResult:
    """把任意图片规范成 PNG 预览；渲染不出来时在 ``reason`` 里说明卡在哪。

    ``reason`` 不是装饰：没有它，"这个格式我们不支持"和"这张图坏了"在下游长得一模一
    样，而前者要补转换器、后者只能认了。
    """
    if not data:
        return PreviewResult(reason="empty_asset")
    detected = sniff_media_type(data, media_type)
    if detected in POSTSCRIPT_TYPES:
        if not ghostscript:
            return PreviewResult(media_type=detected, reason="ghostscript_missing")
        png = render_postscript(data, ghostscript, dpi)
        reason = "" if png else "ghostscript_failed"
        return PreviewResult(png=png, media_type=detected, reason=reason)
    png = render_with_fitz(data, detected, dpi)
    if png is not None:
        return PreviewResult(png=png, media_type=detected)
    return PreviewResult(media_type=detected, reason="unsupported_format")


def render_page_region(
    page: fitz.Page, bbox: tuple[float, float, float, float], dpi: int = 180
) -> bytes:
    """把 PDF point 坐标区域渲染为 PNG；区域会裁剪到页面边界。"""
    rectangle = fitz.Rect(bbox) & page.rect
    if rectangle.is_empty or rectangle.width <= 0 or rectangle.height <= 0:
        raise ValueError(f"Empty PDF region: {bbox}")
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
        clip=rectangle,
        alpha=False,
    )
    return pixmap.tobytes("png")

"""把 TeX 原始图片或 PDF 区域规范成 VLM 可读取的 PNG 预览。"""

import fitz

from athena.research.paper_markdown.document import ParsedVisual
from athena.research.paper_markdown.interfaces import VisualInterpretation
from athena.research.paper_markdown.schemas import PaperVisual

FITZ_FILE_TYPES = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/svg+xml": "svg",
}


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


def render_preview(data: bytes, media_type: str | None, dpi: int = 180) -> bytes | None:
    """将 PDF/SVG/常见位图的第一页规范为 PNG；EPS 等不支持格式返回 ``None``。"""
    file_type = FITZ_FILE_TYPES.get(media_type or "")
    if file_type is None:
        return None
    try:
        with fitz.open(stream=data, filetype=file_type) as document:
            if len(document) == 0:
                return None
            page = document[0]
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False
            )
            return pixmap.tobytes("png")
    except (RuntimeError, ValueError):
        # 图像格式不受支持或内容损坏 → 保留原始证据但不生成预览
        return None


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

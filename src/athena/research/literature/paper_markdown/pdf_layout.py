"""PDF block representation and deterministic multi-column reading order."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class PdfBlock:
    """Normalized text, image, or table block on one PDF page."""

    page_number: int
    bbox: tuple[float, float, float, float]
    block_type: str
    text: str = ""
    max_font_size: float = 0.0
    bold: bool = False
    image_bytes: bytes | None = None
    image_extension: str | None = None
    heading_level: int | None = None
    caption_kind: str | None = None
    caption_label: str | None = None
    caption_text: str = ""
    table_markdown: str = ""
    source_order: int = 0
    repair_issue_codes: list[str] = field(default_factory=list)
    line_count: int = 1

    @property
    def width(self) -> float:
        """Return block width in PDF points."""
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        """Return block height in PDF points."""
        return self.bbox[3] - self.bbox[1]


def order_band(blocks: list[PdfBlock], page_width: float) -> list[PdfBlock]:
    """Order a vertical page band by left column and then right column."""
    center = page_width / 2
    left = [block for block in blocks if (block.bbox[0] + block.bbox[2]) / 2 < center]
    right = [block for block in blocks if block not in left]
    if not left or not right:
        return sorted(
            blocks, key=lambda block: (block.bbox[1], block.bbox[0], block.source_order)
        )
    return sorted(left, key=lambda block: (block.bbox[1], block.bbox[0])) + sorted(
        right, key=lambda block: (block.bbox[1], block.bbox[0])
    )


def reading_order(blocks: list[PdfBlock], page_width: float) -> list[PdfBlock]:
    """Split on spanning blocks and order each remaining multi-column band."""
    spanning = sorted(
        [block for block in blocks if block.width >= page_width * 0.68],
        key=lambda block: (block.bbox[1], block.bbox[0]),
    )
    if not spanning:
        return order_band(blocks, page_width)
    result: list[PdfBlock] = []
    consumed: set[int] = set()
    cursor = 0.0
    for wide in spanning:
        band = [
            block
            for block in blocks
            if id(block) not in consumed
            and block is not wide
            and block.bbox[1] >= cursor
            and block.bbox[3] <= wide.bbox[1] + 2
        ]
        result.extend(order_band(band, page_width))
        consumed.update(id(block) for block in band)
        result.append(wide)
        consumed.add(id(wide))
        cursor = max(cursor, wide.bbox[3])
    remaining = [block for block in blocks if id(block) not in consumed]
    result.extend(order_band(remaining, page_width))
    return result

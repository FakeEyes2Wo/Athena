"""Pure PDF table and reference transformations."""

import re
from dataclasses import dataclass, field

NUMERIC_CITATION = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]")
PARENTHETICAL_CITATION = re.compile(r"\(([^()]*(?:19|20)\d{2}[a-z]?[^()]*)\)")
AUTHOR_YEAR_ITEM = re.compile(
    r"^(.+?),\s*((?:19|20)\d{2}[a-z]?(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*)\s*$"
)
DISPLAY_REFERENCE_GROUP = re.compile(
    r"\b(?P<kind>Figure|Fig\.?|Table)\s+"
    r"(?P<numbers>[A-Za-z]?\d+(?:[.-]\d+)*"
    r"(?:\s*(?:,\s*|(?:and|&)\s+)"
    r"[A-Za-z]?\d+(?:[.-]\d+)*)*)",
    re.IGNORECASE,
)


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
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


def _order_band(blocks: list[PdfBlock], page_width: float) -> list[PdfBlock]:
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
        return _order_band(blocks, page_width)
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
        result.extend(_order_band(band, page_width))
        consumed.update(id(block) for block in band)
        result.append(wide)
        consumed.add(id(wide))
        cursor = max(cursor, wide.bbox[3])
    result.extend(
        _order_band(
            [block for block in blocks if id(block) not in consumed], page_width
        )
    )
    return result


def markdown_table(rows: list[list[str | None]]) -> str:
    """Convert a PyMuPDF cell matrix into Markdown."""
    cleaned = [
        [re.sub(r"\s+", " ", (cell or "")).strip().replace("|", "\\|") for cell in row]
        for row in rows
    ]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    normalized = [row + [""] * (width - len(row)) for row in cleaned]
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
    return "\n".join(lines)


def citation_keys(text: str) -> list[str]:
    """Extract numeric and author-year citation markers from PDF text."""
    keys = [
        match.group(1).replace(" ", "") for match in NUMERIC_CITATION.finditer(text)
    ]
    for group in PARENTHETICAL_CITATION.findall(text):
        for raw_item in group.split(";"):
            item = AUTHOR_YEAR_ITEM.match(raw_item.strip())
            if item is None:
                continue
            author = item.group(1).strip()
            for year in re.findall(r"(?:19|20)\d{2}[a-z]?", item.group(2)):
                keys.append(f"{author}, {year}")
    return list(dict.fromkeys(keys))


def display_reference_keys(text: str) -> list[str]:
    """Extract and expand Figure/Table display references."""
    keys: list[str] = []
    for match in DISPLAY_REFERENCE_GROUP.finditer(text):
        prefix = "Table" if match.group("kind").lower() == "table" else "Figure"
        numbers = re.findall(r"[A-Za-z]?\d+(?:[.-]\d+)*", match.group("numbers"))
        keys.extend(f"{prefix} {number}" for number in numbers)
    return list(dict.fromkeys(keys))

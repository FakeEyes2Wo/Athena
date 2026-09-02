"""Pure PDF table and reference transformations."""

import re

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

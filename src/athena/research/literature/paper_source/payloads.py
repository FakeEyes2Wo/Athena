"""Pure payload recognition, source locators, and conversion requests."""

import difflib
import gzip
import io
import re
import tarfile
import zipfile
from dataclasses import dataclass

from athena.core.contracts import ArtifactRef
from athena.research.literature.contracts import ProcessingDiagnostic, TexSourceFormat
from athena.research.literature.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourcePolicy,
    PaperSourceRecord,
)

TEX_MARKERS = (b"\\documentclass", b"\\begin{document}", b"\\section", b"\\input{")
TEX_FORMATS = frozenset({"auto", "tar", "tar.gz", "zip", "gzip", "plain"})
PDF_HINT_KINDS = frozenset({"oa_pdf", "publisher_pdf"})
TITLE_MATCH_THRESHOLD = 0.75
MAX_PDF_CANDIDATES = 3
MAX_SOURCE_BYTES = 256 * 1024 * 1024
NON_ALNUM = re.compile(r"[^0-9a-z]+")


class PayloadError(ValueError):
    """Raised when a compressed source payload cannot be inspected safely."""


def diagnostic(level: str, code: str, message: str) -> ProcessingDiagnostic:
    """Construct a stable source diagnostic record."""
    return ProcessingDiagnostic(level=level, code=code, message=message)


def _is_tar_payload(payload: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*"):
            return True
    except tarfile.ReadError:
        return False


def _decompress_gzip(payload: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as archive:
            decompressed = archive.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        raise PayloadError("Invalid gzip TeX source.") from error
    if len(decompressed) > MAX_SOURCE_BYTES:
        raise PayloadError("Gzip TeX source exceeds the 256 MiB safety limit.")
    return decompressed


def decompress_gzip(payload: bytes) -> bytes:
    """Decompress a gzip source payload with the source-size safety limit."""
    return _decompress_gzip(payload)


def is_tar_payload(payload: bytes) -> bool:
    """Return whether bytes contain a readable tar archive."""
    return _is_tar_payload(payload)


def gzip_format(payload: bytes) -> TexSourceFormat | None:
    """Distinguish gzip-wrapped tar archives from plain compressed TeX."""
    try:
        inner = _decompress_gzip(payload)
    except PayloadError:
        return None
    return "tar.gz" if _is_tar_payload(inner) else "gzip"


def looks_like_tex(payload: bytes) -> bool:
    """Recognize plain TeX while rejecting HTML error pages."""
    head = payload[:8192].lstrip()
    if head.startswith(b"<") or b"<html" in head.lower():
        return False
    return any(marker in payload for marker in TEX_MARKERS)


def sniff_payload(payload: bytes) -> tuple[str, TexSourceFormat | None]:
    """Classify source bytes as PDF, TeX package, or unknown content."""
    if not payload:
        return "unknown", None
    if payload.startswith(b"%PDF-"):
        return "pdf", None
    if zipfile.is_zipfile(io.BytesIO(payload)):
        return "tex", "zip"
    if payload.startswith(b"\x1f\x8b"):
        detected = gzip_format(payload)
        return ("tex", detected) if detected else ("unknown", None)
    if _is_tar_payload(payload):
        return "tex", "tar"
    if looks_like_tex(payload):
        return "tex", "plain"
    return "unknown", None


def normalize_title(value: str) -> str:
    """Normalize title text for cross-source comparison."""
    return NON_ALNUM.sub(" ", value.lower()).strip()


def title_similarity(left: str, right: str) -> float:
    """Return normalized title similarity, treating missing titles as unknown."""
    first = normalize_title(left)
    second = normalize_title(right)
    if not first or not second:
        return 1.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def pdf_hint_urls(paper: PaperRef) -> list[str]:
    """Return distinct HTTP PDF hints in upstream order."""
    return list(
        dict.fromkeys(hint.url for hint in paper.hints if hint.kind in PDF_HINT_KINDS)
    )


def openalex_locator(identity: PaperIdentity) -> str:
    """Select the most stable OpenAlex lookup locator available."""
    if identity.openalex_id:
        return identity.openalex_id
    if identity.doi:
        return f"doi:{identity.doi}"
    if identity.pmid:
        return f"pmid:{identity.pmid}"
    return ""


@dataclass(slots=True)
class FetchedPayload:
    """One validated downloaded or cached source payload."""

    kind: str
    ref: ArtifactRef
    channel: str
    locator: str
    tex_format: TexSourceFormat | None = None
    cache_hit: bool = False


@dataclass(slots=True)
class ChannelOutcome:
    """Result of one source channel attempt."""

    payload: FetchedPayload | None = None
    attempted: bool = False


def conversion_request(
    record: PaperSourceRecord,
    payload: FetchedPayload,
    policy: PaperSourcePolicy,
) -> dict[str, object]:
    """Build the immutable request consumed by the Markdown converter."""
    return {
        "tex_source_ref": record.tex_source_ref,
        "tex_source_format": record.tex_source_format or "auto",
        "tex_entrypoint": None,
        "pdf_ref": record.pdf_ref,
        "paper_id": record.paper_key,
        "metadata": record.metadata,
        "visual_policy": policy.visual_policy,
        "chunking": policy.chunking.model_dump(),
    }


__all__ = [
    "MAX_PDF_CANDIDATES",
    "PDF_HINT_KINDS",
    "TEX_FORMATS",
    "TITLE_MATCH_THRESHOLD",
    "ChannelOutcome",
    "FetchedPayload",
    "conversion_request",
    "decompress_gzip",
    "diagnostic",
    "fetch_status",
    "gzip_format",
    "is_tar_payload",
    "looks_like_tex",
    "normalize_title",
    "openalex_locator",
    "pdf_hint_urls",
    "sniff_payload",
    "title_similarity",
]


def fetch_status(outcome: ChannelOutcome) -> str:
    """Classify a channel outcome as fetched, failed, or policy-skipped."""
    if outcome.payload:
        return "fetched"
    return "failed" if outcome.attempted else "skipped"

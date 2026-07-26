"""arXiv 优先的论文取源工具，为 paper_markdown 提供版本固定的 TeX/PDF 输入。"""

from athena.research.paper_source.arxiv import (
    ArxivClient,
    ArxivMetadata,
    ArxivResolution,
)
from athena.research.paper_source.fetcher import (
    LocatorCache,
    PaperSourceFetcher,
    sniff_payload,
)
from athena.research.paper_source.http import (
    HostRateLimiter,
    HttpResponse,
    HttpTransport,
    HttpTransportError,
    UrllibTransport,
)
from athena.research.paper_source.openalex import OpenAlexClient, OpenAlexWork
from athena.research.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourcePolicy,
    PaperSourceRecord,
    PaperSourceRequest,
    PaperSourceResult,
    PaperSourceStats,
    SourceHint,
    normalize_arxiv_id,
    normalize_doi,
)
from athena.research.paper_source.tool import PaperFetchTool

__all__ = [
    "PaperIdentity",
    "PaperRef",
    "SourceHint",
    "PaperSourcePolicy",
    "PaperSourceRequest",
    "PaperSourceRecord",
    "PaperSourceStats",
    "PaperSourceResult",
    "normalize_arxiv_id",
    "normalize_doi",
    "HttpResponse",
    "HttpTransport",
    "HttpTransportError",
    "UrllibTransport",
    "HostRateLimiter",
    "ArxivClient",
    "ArxivMetadata",
    "ArxivResolution",
    "OpenAlexClient",
    "OpenAlexWork",
    "LocatorCache",
    "PaperSourceFetcher",
    "sniff_payload",
    "PaperFetchTool",
]

"""AcademicSurvey 的四个真实论文元数据通道。"""

from athena.research.academic_survey.channels.arxiv import ArxivChannel
from athena.research.academic_survey.channels.openalex import OpenAlexChannel
from athena.research.academic_survey.channels.pubmed import PubMedChannel
from athena.research.academic_survey.channels.semantic_scholar import (
    SemanticScholarChannel,
)
from athena.research.paper_source.http import HostRateLimiter
from athena.storage.artifact_store import ArtifactStore


def build_default_channels(
    artifacts: ArtifactStore,
    *,
    http: HostRateLimiter | None = None,
    contact_email: str | None = None,
    openalex_api_key: str | None = None,
    semantic_scholar_api_key: str | None = None,
    pubmed_api_key: str | None = None,
):
    """构造共享限流器的生产通道；凭据只由调用方显式注入。"""
    limiter = http or HostRateLimiter(
        default_interval=0.2,
        bucket_intervals={
            "api.semanticscholar.org": 1.0,
            "eutils.ncbi.nlm.nih.gov": 0.34,
        },
        contact_email=contact_email,
    )
    return {
        "arxiv": ArxivChannel(limiter, artifacts),
        "openalex": OpenAlexChannel(
            limiter,
            artifacts,
            contact_email=contact_email,
            api_key=openalex_api_key,
        ),
        "semantic_scholar": SemanticScholarChannel(
            limiter,
            artifacts,
            api_key=semantic_scholar_api_key,
        ),
        "pubmed": PubMedChannel(
            limiter,
            artifacts,
            api_key=pubmed_api_key,
            contact_email=contact_email,
        ),
    }


__all__ = [
    "ArxivChannel",
    "OpenAlexChannel",
    "PubMedChannel",
    "SemanticScholarChannel",
    "build_default_channels",
]

"""PaperScout：以 search/expand 多轮决策收集论文的检索 Agent。"""

from athena.research.paper_scout.agent import PaperScoutAgent, collect_tool_calls
from athena.research.paper_scout.backends import (
    ArxivSearchBackend,
    BackendError,
    ReferenceBackend,
    SearchBackend,
    SemanticScholarBackend,
    build_default_backends,
    paper_key_for,
    within_cutoff,
)
from athena.research.paper_scout.pool import (
    PaperPool,
    has_retrievable_source,
    truncate_abstract,
)
from athena.research.paper_scout.schemas import (
    ACCEPT_THRESHOLD,
    PASA_RETAIN_THRESHOLD,
    RETAIN_THRESHOLD,
    PaperScoutResult,
    ScoutAction,
    ScoutCorpus,
    ScoutPaper,
    ScoutRequest,
    ScoutStats,
)
from athena.research.paper_scout.scorer import (
    GradedRelevanceScorer,
    RelevanceScorer,
    TokenProbabilityScorer,
    parse_grades,
)
from athena.research.paper_scout.session import ScoutSession, process_reward
from athena.research.paper_scout.tool import (
    PaperScoutExpandTool,
    PaperScoutSearchTool,
)

__all__ = [
    "ACCEPT_THRESHOLD",
    "PASA_RETAIN_THRESHOLD",
    "RETAIN_THRESHOLD",
    "ArxivSearchBackend",
    "BackendError",
    "GradedRelevanceScorer",
    "PaperPool",
    "has_retrievable_source",
    "PaperScoutAgent",
    "PaperScoutExpandTool",
    "PaperScoutResult",
    "PaperScoutSearchTool",
    "ReferenceBackend",
    "RelevanceScorer",
    "ScoutAction",
    "ScoutCorpus",
    "ScoutPaper",
    "ScoutRequest",
    "ScoutSession",
    "ScoutStats",
    "SearchBackend",
    "SemanticScholarBackend",
    "TokenProbabilityScorer",
    "build_default_backends",
    "collect_tool_calls",
    "paper_key_for",
    "parse_grades",
    "process_reward",
    "truncate_abstract",
    "within_cutoff",
]

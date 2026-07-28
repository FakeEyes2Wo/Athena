"""AcademicSurvey：面向 paper_source 的 SPAR 论文发现 Agent。"""

from athena.research.academic_survey.chains import (
    DEFAULT_PROMPTS,
    LangChainSurveyChains,
)
from athena.research.academic_survey.cache import (
    CachedChannelAdapter,
    MemorySurveyCache,
    ReplayChannelAdapter,
)
from athena.research.academic_survey.channels import build_default_channels
from athena.research.academic_survey.graph import build_survey_graph
from athena.research.academic_survey.interfaces import ChannelAdapter, SurveyChains
from athena.research.academic_survey.schemas import (
    AcademicSurveyResult,
    CandidateObservation,
    QueryPlan,
    SurveyCorpus,
    SurveyRequest,
)

__all__ = [
    "AcademicSurveyResult",
    "CandidateObservation",
    "CachedChannelAdapter",
    "ChannelAdapter",
    "DEFAULT_PROMPTS",
    "LangChainSurveyChains",
    "MemorySurveyCache",
    "QueryPlan",
    "ReplayChannelAdapter",
    "SurveyChains",
    "SurveyCorpus",
    "SurveyRequest",
    "build_survey_graph",
    "build_default_channels",
]

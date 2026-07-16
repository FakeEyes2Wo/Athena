"""AcademicSurvey：给定题目的文献检索基础设施（SPAR 回路 + GEPA 离线优化）。"""

from athena.research.academic_survey.channels import ChannelAdapter
from athena.research.academic_survey.optimizer import SurveyPromptOptimizer
from athena.research.academic_survey.schemas import (
    CriterionJudgment,
    PaperContent,
    PaperRecord,
    PromptBundle,
    RelevanceJudgment,
    SurveyCorpus,
    SurveyRequest,
)
from athena.research.academic_survey.service import AcademicSurveyService, rrf_fuse

__all__ = [
    "AcademicSurveyService",
    "ChannelAdapter",
    "CriterionJudgment",
    "PaperContent",
    "PaperRecord",
    "PromptBundle",
    "RelevanceJudgment",
    "SurveyCorpus",
    "SurveyPromptOptimizer",
    "SurveyRequest",
    "rrf_fuse",
]

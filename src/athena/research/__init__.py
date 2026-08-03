"""Research 链路：论文检索、取源、转换、RAG 检索，以及把四段串起来的组合根。"""

from athena.research.pipeline import (
    PaperOutcome,
    StageTimings,
    SurveyPipeline,
    SurveyReport,
    SurveyRequest,
    run_survey,
)
from athena.research.wiring import (
    OpenAIEmbedder,
    ResearchStack,
    VisionInterpreter,
    build_artifact_store,
    build_research_stack,
    build_research_tools,
    resolve_model,
)

__all__ = [
    "OpenAIEmbedder",
    "PaperOutcome",
    "ResearchStack",
    "StageTimings",
    "SurveyPipeline",
    "SurveyReport",
    "SurveyRequest",
    "VisionInterpreter",
    "build_artifact_store",
    "build_research_stack",
    "build_research_tools",
    "resolve_model",
    "run_survey",
]

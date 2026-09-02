"""Academic Survey 全链路：一句主题 → 一个可检索的论文语料库。

四段串起来：``paper_scout`` 多轮检索出候选，``paper_source`` 取回 TeX/PDF，
``paper_markdown`` 转成带图表解读的 Markdown，``paper_rag`` 建索引。段与段之间的
交接契约本来就是闭合的，``pipeline`` 只负责把它们依次推进并记账。

对外三个入口：``run_survey``（库调用）、``PaperSurveyTool``（交给 Agent）、
``athena.cli`` 的 ``survey`` 子命令（人用）。三者共用 ``build_survey_stack``
这一个组合根。
"""

from athena.research.literature.survey.pipeline import (
    PaperOutcome,
    StageTimings,
    SurveyPipeline,
    SurveyReport,
    SurveyRequest,
    run_survey,
)
from athena.research.literature.survey.tool import SURVEY_TOOL_NAME, PaperSurveyTool
from athena.research.literature.survey.wiring import (
    OpenAIEmbedder,
    SurveyStack,
    VisionInterpreter,
    build_survey_stack,
    build_survey_tools,
)

__all__ = [
    "OpenAIEmbedder",
    "PaperOutcome",
    "PaperSurveyTool",
    "SURVEY_TOOL_NAME",
    "StageTimings",
    "SurveyPipeline",
    "SurveyReport",
    "SurveyRequest",
    "SurveyStack",
    "VisionInterpreter",
    "build_survey_stack",
    "build_survey_tools",
    "run_survey",
]

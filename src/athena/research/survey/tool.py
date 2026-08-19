"""Academic Survey 的 Athena 工具边界：一次调用换一个可检索的语料库。

调用方给一句主题，拿回 ``corpus_ref``；之后用 ``build_survey_tools`` 里那几个检索
算子读它。四段流水线（检索 → 取源 → 转换 → 建索引）合成一个工具而不是拆成四个，
是因为中间三段之间没有任何需要模型决策的地方——交接契约本来就是闭合的，拆开只会让
模型替流程做它做不好的记账。

``concurrency_safe=False``：这一次调用会跑满速率受限的三个上游站点、下载几十兆
字节、并发调用视觉与编码模型。同一个 turn 里并行两次没有任何收益，只会互相抢限流
配额。
"""

from typing import TYPE_CHECKING

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.survey.pipeline import SurveyRequest, run_survey

if TYPE_CHECKING:
    from athena.research.survey.wiring import SurveyStack

SURVEY_TOOL_NAME = "paper_survey"


class PaperSurveyTool(BaseTool):
    """按主题跑完整条文献链路，返回语料引用与这次运行的成本账。

    依赖由组合根注入（``SurveyStack``），工具本身不读环境变量、不建客户端。
    """

    spec = ToolSpec(
        name=SURVEY_TOOL_NAME,
        description=(
            "Build a searchable corpus of academic papers on a topic. Retrieves "
            "candidate papers, fetches their TeX or PDF source, converts them to "
            "Markdown with figures and tables interpreted, and indexes the result. "
            "Returns corpus_ref plus per-paper outcomes. Pass corpus_ref to "
            "paper_keyword_search, paper_semantic_search and paper_chunk_read to "
            "read the corpus. This runs for several minutes and downloads tens of "
            "megabytes, so call it once per topic and reuse the corpus_ref."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Natural language survey topic.",
                },
                "max_papers": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": (
                        "Papers carried through to the corpus. Cost grows roughly "
                        "linearly downstream of retrieval."
                    ),
                },
                "published_to": {
                    "type": "string",
                    "description": (
                        "Inclusive ISO date upper bound, for reproducing a survey as "
                        "of a past date. Empty means no bound."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    def __init__(self, stack: "SurveyStack") -> None:
        self.stack = stack

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """跑一次全链路；一篇都没转换成功时把报告当作失败返回。"""
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        request = SurveyRequest(
            query=query,
            max_papers=_resolve_max_papers(input),
            published_to=str(input.get("published_to") or ""),
        )
        report = await run_survey(self.stack, request)
        # 逐篇明细留在 artifact 里：一次 10 篇的报告 JSON 有几千字符，塞进对话历史
        # 挤掉的是模型真正要读的论文内容
        report_ref = await self.stack.artifacts.put_text(report.model_dump_json())
        data = {
            "corpus_ref": report.corpus_ref,
            "report_ref": report_ref,
            "status": report.status,
            "papers_indexed": sum(1 for item in report.papers if item.indexed),
            "papers_converted": report.converted(),
            "papers_fetched": report.fetched,
            "pool_size": report.scout.pool_size,
            "warnings": report.warnings,
        }
        if report.corpus_ref is None:
            return ToolResult(
                data=data,
                success=False,
                error=(
                    f"No corpus was built (status={report.status}); "
                    f"see report_ref {report_ref} for per-paper reasons."
                ),
            )
        return ToolResult(data=data)


def _resolve_max_papers(input: dict) -> int:
    """取出并夹紧 ``max_papers``；``BaseTool`` 不校验 input schema，边界必须兜底。"""
    value = input.get("max_papers")
    if not isinstance(value, int) or value < 1:
        return SurveyRequest.model_fields["max_papers"].default
    return min(value, 50)

"""Kaggle 工具边界：五个算子 + 一个聚合算子 ``kaggle_run``。

这些工具只负责「取数」（元数据、下载、notebook 证据、提交）；EDA 与 SOTA
分析由 prepare/ideator 等 agent 用自己的 workspace + shell 代码生成流程完成。
"""

from typing import TYPE_CHECKING

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.kaggle.client import author_name, pick_first
from athena.kaggle.pipeline import run_kaggle
from athena.kaggle.schemas import KaggleRunRequest

if TYPE_CHECKING:
    from athena.kaggle.wiring import KaggleStack

KAGGLE_LIST_COMPETITIONS = "kaggle_list_competitions"
KAGGLE_GET_COMPETITION = "kaggle_get_competition"
KAGGLE_LIST_NOTEBOOKS = "kaggle_list_notebooks"
KAGGLE_DOWNLOAD_DATA = "kaggle_download_data"
KAGGLE_RUN = "kaggle_run"
KAGGLE_SUBMIT = "kaggle_submit"


def _schema(props: dict[str, str], *required: str) -> dict:
    """构造宽松的 JSON Schema：只声明字段类型，其余交给 LLM 自主决定。"""
    return {
        "type": "object",
        "properties": {name: {"type": typ} for name, typ in props.items()},
        "required": list(required),
    }


class KaggleListCompetitionsTool(BaseTool):
    spec = ToolSpec(
        name=KAGGLE_LIST_COMPETITIONS,
        description=(
            "List Kaggle competitions, optionally filtered by a search term. Returns "
            "each competition's slug (ref), title, deadline, reward and team count. "
            "Use the returned ref with kaggle_get_competition to inspect one competition."
        ),
        input_schema=_schema(
            {"search": "string", "page": "integer", "sort_by": "string"}
        ),
    )

    def __init__(self, stack: "KaggleStack") -> None:
        self.stack = stack

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        raw = await self.stack.client.list_competitions(
            search=str(input.get("search", "")),
            page=_as_int(input.get("page"), 1),
            sort_by=str(input.get("sort_by", "latestDeadline")),
        )
        summaries = [
            {
                "ref": i.get("ref", ""),
                "title": i.get("title", ""),
                "deadline": i.get("deadline", ""),
                "reward": i.get("reward", ""),
                "team_count": pick_first(i, "teamCount", "team_count", default=0),
            }
            for i in raw
        ]
        return ToolResult(data={"competitions": summaries, "count": len(summaries)})


class KaggleGetCompetitionTool(BaseTool):
    spec = ToolSpec(
        name=KAGGLE_GET_COMPETITION,
        description=(
            "Get one Kaggle competition's overview: title, description, evaluation "
            "metric, tags, category, organization, team/kernel counts, daily "
            "submission limit, and its data files. Pass the competition slug (ref) "
            "as returned by kaggle_list_competitions."
        ),
        input_schema=_schema({"competition": "string"}, "competition"),
    )

    def __init__(self, stack: "KaggleStack") -> None:
        self.stack = stack

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        raw = await self.stack.client.get_competition(ref)
        files = await self.stack.client.list_data_files(ref)
        return ToolResult(
            data={
                "ref": ref,
                "title": raw.get("title", ""),
                "description": raw.get("description", ""),
                "evaluation_metric": pick_first(
                    raw, "evaluationMetric", "evaluation_metric"
                ),
                "category": raw.get("category", ""),
                "tags": _tag_names(raw.get("tags")),
                "organization_name": pick_first(
                    raw, "organizationName", "organization_name"
                ),
                "team_count": pick_first(raw, "teamCount", "team_count", default=0),
                "kernel_count": pick_first(
                    raw, "kernelCount", "kernel_count", default=0
                ),
                "max_daily_submissions": pick_first(
                    raw, "maxDailySubmissions", "max_daily_submissions", default=0
                ),
                "max_team_size": pick_first(
                    raw, "maxTeamSize", "max_team_size", default=0
                ),
                "url": raw.get("url", ""),
                "data_files": [i.get("name", "") for i in files if isinstance(i, dict)],
            }
        )


class KaggleListNotebooksTool(BaseTool):
    spec = ToolSpec(
        name=KAGGLE_LIST_NOTEBOOKS,
        description=(
            "List public Kaggle notebooks for a competition, sorted by votes, to find "
            "current SOTA approaches. Returns each notebook's title, author, votes and "
            "url. Use this to gather evidence before implementing a solution."
        ),
        input_schema=_schema(
            {"competition": "string", "max_results": "integer"}, "competition"
        ),
    )

    def __init__(self, stack: "KaggleStack") -> None:
        self.stack = stack

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        raw = await self.stack.client.list_notebooks(ref, sort_by="hotness")
        limit = max(1, min(_as_int(input.get("max_results"), 10), 50))
        items = [
            {
                "title": i.get("title", ""),
                "author": author_name(i.get("author", "")),
                "votes": pick_first(i, "totalVotes", "total_votes", default=0),
                "url": i.get("url", "")
                or (
                    f"https://www.kaggle.com/code/{i.get('ref')}"
                    if i.get("ref")
                    else ""
                ),
            }
            for i in raw[:limit]
        ]
        return ToolResult(data={"notebooks": items, "count": len(items)})


class KaggleDownloadDataTool(BaseTool):
    spec = ToolSpec(
        name=KAGGLE_DOWNLOAD_DATA,
        description=(
            "Download and extract a Kaggle competition's data files into the workspace, "
            "unless the supervisor disabled local download. Returns the extracted local "
            "file paths and a data manifest reference."
        ),
        input_schema=_schema(
            {"competition": "string", "download_subdir": "string"}, "competition"
        ),
        concurrency_safe=False,
    )

    def __init__(self, stack: "KaggleStack") -> None:
        self.stack = stack

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        if not self.stack.download:
            return ToolResult(
                data={
                    "downloaded": False,
                    "reason": "supervisor disabled local download",
                }
            )
        report = await run_kaggle(
            self.stack.client,
            self.stack.artifacts,
            self.stack.download_root,
            KaggleRunRequest(
                competition=ref,
                download_subdir=str(input.get("download_subdir", "")),
                max_notebooks=0,
            ),
        )
        if report.competition is None:
            return ToolResult(
                data={"warnings": report.warnings},
                success=False,
                error="connect failed; see warnings.",
            )
        return ToolResult(
            data={
                "downloaded_files": report.downloaded_files,
                "data_manifest_ref": report.data_manifest_ref,
                "warnings": report.warnings,
            },
            artifacts=[report.data_manifest_ref] if report.data_manifest_ref else [],
        )


class KaggleRunTool(BaseTool):
    spec = ToolSpec(
        name=KAGGLE_RUN,
        description=(
            "Fetch everything needed to start a Kaggle competition in one call: "
            "competition metadata, downloaded data file paths (unless the supervisor "
            "disabled local download), a data manifest reference, and top public "
            "notebooks as evidence. Run EDA and build your baseline yourself afterward "
            "using your own workspace and shell tools, exactly as for a local dataset."
        ),
        input_schema=_schema(
            {
                "competition": "string",
                "download_subdir": "string",
                "max_notebooks": "integer",
            },
            "competition",
        ),
        concurrency_safe=False,
    )

    def __init__(self, stack: "KaggleStack") -> None:
        self.stack = stack

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        request = KaggleRunRequest(
            competition=ref,
            download_subdir=str(input.get("download_subdir", "")),
            max_notebooks=max(0, min(_as_int(input.get("max_notebooks"), 10), 50)),
            download_data=self.stack.download,
        )
        report = await run_kaggle(
            self.stack.client,
            self.stack.artifacts,
            self.stack.download_root,
            request,
        )
        data = {
            "status": report.status,
            "competition": (
                report.competition.model_dump() if report.competition else None
            ),
            "downloaded_files": report.downloaded_files,
            "data_manifest_ref": report.data_manifest_ref,
            "notebooks": [n.model_dump() for n in report.notebooks],
            "http_requests": report.http_requests,
            "warnings": report.warnings,
        }
        if report.status == "empty":
            return ToolResult(
                data=data, success=False, error="competition could not be loaded."
            )
        refs = [report.data_manifest_ref] if report.data_manifest_ref else []
        return ToolResult(data=data, artifacts=refs)


class KaggleSubmitTool(BaseTool):
    spec = ToolSpec(
        name=KAGGLE_SUBMIT,
        description=(
            "Submit a local predictions CSV to a Kaggle competition. Pass the "
            "competition slug and the absolute path of the submission file (usually "
            "the final predictions CSV produced by VALIDATE)."
        ),
        input_schema=_schema(
            {"competition": "string", "file_path": "string"}, "competition", "file_path"
        ),
        concurrency_safe=False,
    )

    def __init__(self, stack: "KaggleStack") -> None:
        self.stack = stack

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        file_path = input.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError("file_path must be a non-empty string.")
        result = await self.stack.client.submit_submission(ref, file_path.strip())
        return ToolResult(data=result)


def _tag_names(value: object) -> list[str]:
    """归一化 ``tags`` 字段为字符串列表（兼容 ``[{"name": ...}]`` 与 ``["..."]``）。"""
    if not isinstance(value, list):
        return []
    names = []
    for item in value:
        if isinstance(item, dict):
            if item.get("name"):
                names.append(str(item["name"]))
        elif isinstance(item, str) and item:
            names.append(item)
    return names


def _competition_ref(input: dict) -> str:
    value = input.get("competition")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("competition must be a non-empty string.")
    return value.strip()


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default

"""Kaggle 工具边界：notebook/discussion 证据算子 + 一个聚合算子 ``kaggle_run``。

这些工具只负责「取数」（元数据、下载、notebook/discussion 证据、提交）；EDA
与 SOTA 分析由 prepare/ideator 等 agent 用自己的 workspace + shell 代码生成流程完成。
"""

import urllib.parse

from athena.core.tool import StackTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.kaggle.client import author_name, pick_first
from athena.kaggle.pipeline import run_kaggle
from athena.kaggle.schemas import KaggleRunRequest

KAGGLE_LIST_COMPETITIONS = "kaggle_list_competitions"
KAGGLE_GET_COMPETITION = "kaggle_get_competition"
KAGGLE_LIST_NOTEBOOKS = "kaggle_list_notebooks"
KAGGLE_GET_NOTEBOOK = "kaggle_get_notebook"
KAGGLE_LIST_DISCUSSIONS = "kaggle_list_discussions"
KAGGLE_GET_DISCUSSION = "kaggle_get_discussion"
KAGGLE_DOWNLOAD_DATA = "kaggle_download_data"
KAGGLE_RUN = "kaggle_run"
KAGGLE_SUBMIT = "kaggle_submit"

MAX_NOTEBOOK_SOURCE_CHARS = 120_000
MAX_DISCUSSION_CHARS = 120_000


def _schema(props: dict[str, str], *required: str) -> dict:
    """构造宽松的 JSON Schema：只声明字段类型，其余交给 LLM 自主决定。"""
    return {
        "type": "object",
        "properties": {name: {"type": typ} for name, typ in props.items()},
        "required": list(required),
    }


_COMPETITION_LIST_SCHEMA = _schema(
    {"competition": "string", "max_results": "integer"}, "competition"
)
"""list_notebooks / list_discussions 共用的输入 schema。"""


def _notebook_ref(input: dict) -> str:
    """Accept either a notebook URL or an ``owner/slug`` ref."""
    value = str(
        input.get("notebook") or input.get("ref") or input.get("url") or ""
    ).strip()
    if not value:
        raise ValueError("notebook must be a non-empty string")
    if "kaggle.com/code/" not in value:
        return value
    parts = urllib.parse.urlsplit(value).path.strip("/").split("/")
    if "code" in parts:
        parts = parts[parts.index("code") + 1 :]
    return "/".join(parts[:2]) or value


def _discussion_ref(input: dict) -> str:
    """Accept either a discussion URL or its id/ref."""
    value = str(
        input.get("discussion") or input.get("ref") or input.get("url") or ""
    ).strip()
    if not value:
        raise ValueError("discussion must be a non-empty string")
    if "kaggle.com" not in value:
        return value
    parts = urllib.parse.urlsplit(value).path.strip("/").split("/")
    if "discussion" in parts:
        after = parts[parts.index("discussion") + 1 :]
        if after:
            return "/".join(after)
    return parts[-1] if parts else value


class KaggleListCompetitionsTool(StackTool):
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


class KaggleGetCompetitionTool(StackTool):
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


class KaggleListNotebooksTool(StackTool):
    spec = ToolSpec(
        name=KAGGLE_LIST_NOTEBOOKS,
        description=(
            "List public Kaggle notebooks for a competition, sorted by votes, to find "
            "current SOTA approaches. Returns each notebook's title, author, votes and "
            "url. Use this to gather evidence before implementing a solution."
        ),
        input_schema=_COMPETITION_LIST_SCHEMA,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        raw = await self.stack.client.list_notebooks(ref, sort_by="hotness")
        limit = max(1, min(_as_int(input.get("max_results"), 10), 50))
        items = [
            {
                "ref": i.get("ref", ""),
                "title": i.get("title", ""),
                "author": author_name(i.get("author", "")),
                "votes": pick_first(i, "totalVotes", "total_votes", default=0),
                "version": str(
                    pick_first(
                        i,
                        "currentVersion",
                        "current_version",
                        "version",
                        "versionNumber",
                        default="",
                    )
                ),
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


class KaggleGetNotebookTool(StackTool):
    spec = ToolSpec(
        name=KAGGLE_GET_NOTEBOOK,
        description=(
            "Read a public Kaggle notebook's full source (code + markdown). Pass the "
            "notebook ref or url returned by kaggle_list_notebooks. Returns the source "
            "text; it is truncated to the first "
            f"{MAX_NOTEBOOK_SOURCE_CHARS} characters."
        ),
        input_schema=_schema({"notebook": "string"}, "notebook"),
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _notebook_ref(input)
        source = await self.stack.client.get_notebook(ref)
        truncated = len(source) > MAX_NOTEBOOK_SOURCE_CHARS
        return ToolResult(
            data={
                "notebook": ref,
                "source": source[:MAX_NOTEBOOK_SOURCE_CHARS],
                "truncated": truncated,
                "length": len(source),
            }
        )


class KaggleListDiscussionsTool(StackTool):
    spec = ToolSpec(
        name=KAGGLE_LIST_DISCUSSIONS,
        description=(
            "List public Kaggle competition discussion threads, sorted by hotness, "
            "to find community pitfalls, failed attempts and known tricks. Returns "
            "each thread's ref, title, author, votes, comment count and url. Use "
            "this to gather qualitative evidence before forming hypotheses."
        ),
        input_schema=_COMPETITION_LIST_SCHEMA,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        raw = await self.stack.client.list_discussions(ref, sort_by="hotness")
        limit = max(1, min(_as_int(input.get("max_results"), 10), 50))
        items = [
            {
                "ref": i.get("ref", "")
                or str(i.get("id", ""))
                or str(i.get("threadId", ""))
                or "",
                "title": i.get("title", ""),
                "author": author_name(i.get("author", "")),
                "votes": pick_first(
                    i, "totalVotes", "total_votes", "voteCount", "vote_count", default=0
                ),
                "comment_count": pick_first(
                    i,
                    "totalComments",
                    "total_comments",
                    "commentCount",
                    "comment_count",
                    default=0,
                ),
                "url": i.get("url", ""),
            }
            for i in raw[:limit]
        ]
        return ToolResult(data={"discussions": items, "count": len(items)})


class KaggleGetDiscussionTool(StackTool):
    spec = ToolSpec(
        name=KAGGLE_GET_DISCUSSION,
        description=(
            "Read a public Kaggle discussion thread's title, body and comments. "
            "Pass the discussion ref or url returned by kaggle_list_discussions. "
            "Returns the text; it is truncated to the first "
            f"{MAX_DISCUSSION_CHARS} characters."
        ),
        input_schema=_schema({"discussion": "string"}, "discussion"),
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _discussion_ref(input)
        source = await self.stack.client.get_discussion(ref)
        truncated = len(source) > MAX_DISCUSSION_CHARS
        return ToolResult(
            data={
                "discussion": ref,
                "source": source[:MAX_DISCUSSION_CHARS],
                "truncated": truncated,
                "length": len(source),
            }
        )


class KaggleDownloadDataTool(StackTool):
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
            self.stack,
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


class KaggleRunTool(StackTool):
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

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = _competition_ref(input)
        request = KaggleRunRequest(
            competition=ref,
            download_subdir=str(input.get("download_subdir", "")),
            max_notebooks=max(0, min(_as_int(input.get("max_notebooks"), 10), 50)),
            download_data=self.stack.download,
        )
        report = await run_kaggle(
            self.stack,
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


class KaggleSubmitTool(StackTool):
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

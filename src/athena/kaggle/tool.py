"""Compose Kaggle runtime resources and expose their Athena tools."""

import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool import StackTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.kaggle.auth import KAGGLE_TOKEN_ENV, KaggleCredentials, resolve_credentials
from athena.kaggle.client import KaggleApiClient, author_name, pick_first
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
DOWNLOAD_ROOT_ENV = "ATHENA_KAGGLE_DOWNLOAD_ROOT"
DEFAULT_DOWNLOAD_ROOT = Path.cwd() / "kaggle-data"
DEFAULT_ARTIFACT_ROOT = Path.home() / ".athena" / "artifacts"

_KAGGLE_SLUG_RE = re.compile(
    r"kaggle\.com/(?:competitions|c|t)/([a-z0-9][a-z0-9-]*)", re.IGNORECASE
)


@dataclass(slots=True)
class KaggleStack:
    """Share the Kaggle client, artifact store, download root, and download policy."""

    client: KaggleApiClient
    artifacts: LocalArtifactStore
    download_root: Path
    download: bool = True


def build_kaggle_stack(
    *,
    download_root: str | Path = "",
    credentials: KaggleCredentials | None = None,
    artifacts: LocalArtifactStore | None = None,
    download: bool = True,
) -> KaggleStack:
    """Build the resources shared by Kaggle commands and tools."""
    resolved = credentials or resolve_credentials(
        KaggleCredentials(bearer_token=os.environ.get(KAGGLE_TOKEN_ENV, ""))
    )
    root = Path(
        download_root or os.environ.get(DOWNLOAD_ROOT_ENV, "") or DEFAULT_DOWNLOAD_ROOT
    ).resolve()
    store = artifacts or LocalArtifactStore(
        os.environ.get("ATHENA_ARTIFACT_ROOT", "") or DEFAULT_ARTIFACT_ROOT
    )
    return KaggleStack(
        client=KaggleApiClient(resolved),
        artifacts=store,
        download_root=root,
        download=download,
    )


def kaggle_slug_from_task(task: str) -> str | None:
    """Extract a competition slug from a Kaggle URL embedded in task text."""
    match = _KAGGLE_SLUG_RE.search(task or "")
    return match.group(1) if match else None


def _schema(props: dict[str, str], *required: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": kind} for name, kind in props.items()},
        "required": list(required),
    }


_COMPETITION_LIST_SCHEMA = _schema(
    {"competition": "string", "max_results": "integer"}, "competition"
)


def _notebook_ref(input: dict) -> str:
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


def _limit(input: dict) -> int:
    return max(1, min(_as_int(input.get("max_results"), 10), 50))


def _tag_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item["name"]) if isinstance(item, dict) else item
        for item in value
        if (isinstance(item, dict) and item.get("name"))
        or (isinstance(item, str) and item)
    ]


async def _list_competitions(stack: Any, input: dict) -> ToolResult:
    raw = await stack.client.list_competitions(
        search=str(input.get("search", "")),
        page=_as_int(input.get("page"), 1),
        sort_by=str(input.get("sort_by", "latestDeadline")),
    )
    summaries = [
        {
            "ref": item.get("ref", ""),
            "title": item.get("title", ""),
            "deadline": item.get("deadline", ""),
            "reward": item.get("reward", ""),
            "team_count": pick_first(item, "teamCount", "team_count", default=0),
        }
        for item in raw
    ]
    return ToolResult(data={"competitions": summaries, "count": len(summaries)})


async def _get_competition(stack: Any, input: dict) -> ToolResult:
    ref = _competition_ref(input)
    raw = await stack.client.get_competition(ref)
    files = await stack.client.list_data_files(ref)
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
            "kernel_count": pick_first(raw, "kernelCount", "kernel_count", default=0),
            "max_daily_submissions": pick_first(
                raw, "maxDailySubmissions", "max_daily_submissions", default=0
            ),
            "max_team_size": pick_first(raw, "maxTeamSize", "max_team_size", default=0),
            "url": raw.get("url", ""),
            "data_files": [
                item.get("name", "") for item in files if isinstance(item, dict)
            ],
        }
    )


async def _list_notebooks(stack: Any, input: dict) -> ToolResult:
    raw = await stack.client.list_notebooks(_competition_ref(input), sort_by="hotness")
    items = [
        {
            "ref": item.get("ref", ""),
            "title": item.get("title", ""),
            "author": author_name(item.get("author", "")),
            "votes": pick_first(item, "totalVotes", "total_votes", default=0),
            "version": str(
                pick_first(
                    item,
                    "currentVersion",
                    "current_version",
                    "version",
                    "versionNumber",
                    default="",
                )
            ),
            "url": item.get("url", "")
            or (
                f"https://www.kaggle.com/code/{item.get('ref')}"
                if item.get("ref")
                else ""
            ),
        }
        for item in raw[: _limit(input)]
    ]
    return ToolResult(data={"notebooks": items, "count": len(items)})


async def _get_notebook(stack: Any, input: dict) -> ToolResult:
    ref = _notebook_ref(input)
    source = await stack.client.get_notebook(ref)
    return ToolResult(
        data={
            "notebook": ref,
            "source": source[:MAX_NOTEBOOK_SOURCE_CHARS],
            "truncated": len(source) > MAX_NOTEBOOK_SOURCE_CHARS,
            "length": len(source),
        }
    )


async def _list_discussions(stack: Any, input: dict) -> ToolResult:
    raw = await stack.client.list_discussions(
        _competition_ref(input), sort_by="hotness"
    )
    items = [
        {
            "ref": item.get("ref", "")
            or str(item.get("id", ""))
            or str(item.get("threadId", ""))
            or "",
            "title": item.get("title", ""),
            "author": author_name(item.get("author", "")),
            "votes": pick_first(
                item,
                "totalVotes",
                "total_votes",
                "voteCount",
                "vote_count",
                default=0,
            ),
            "comment_count": pick_first(
                item,
                "totalComments",
                "total_comments",
                "commentCount",
                "comment_count",
                default=0,
            ),
            "url": item.get("url", ""),
        }
        for item in raw[: _limit(input)]
    ]
    return ToolResult(data={"discussions": items, "count": len(items)})


async def _get_discussion(stack: Any, input: dict) -> ToolResult:
    ref = _discussion_ref(input)
    source = await stack.client.get_discussion(ref)
    return ToolResult(
        data={
            "discussion": ref,
            "source": source[:MAX_DISCUSSION_CHARS],
            "truncated": len(source) > MAX_DISCUSSION_CHARS,
            "length": len(source),
        }
    )


async def _download_data(stack: Any, input: dict) -> ToolResult:
    if not stack.download:
        return ToolResult(
            data={
                "downloaded": False,
                "reason": "supervisor disabled local download",
            }
        )
    report = await run_kaggle(
        stack,
        KaggleRunRequest(
            competition=_competition_ref(input),
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


async def _run(stack: Any, input: dict) -> ToolResult:
    report = await run_kaggle(
        stack,
        KaggleRunRequest(
            competition=_competition_ref(input),
            download_subdir=str(input.get("download_subdir", "")),
            max_notebooks=max(0, min(_as_int(input.get("max_notebooks"), 10), 50)),
            download_data=stack.download,
        ),
    )
    data = {
        "status": report.status,
        "competition": (
            report.competition.model_dump() if report.competition else None
        ),
        "downloaded_files": report.downloaded_files,
        "data_manifest_ref": report.data_manifest_ref,
        "notebooks": [notebook.model_dump() for notebook in report.notebooks],
        "http_requests": report.http_requests,
        "warnings": report.warnings,
    }
    if report.status == "empty":
        return ToolResult(
            data=data, success=False, error="competition could not be loaded."
        )
    return ToolResult(
        data=data,
        artifacts=[report.data_manifest_ref] if report.data_manifest_ref else [],
    )


async def _submit(stack: Any, input: dict) -> ToolResult:
    file_path = input.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path must be a non-empty string.")
    result = await stack.client.submit_submission(
        _competition_ref(input), file_path.strip()
    )
    return ToolResult(data=result)


_SERIAL_TOOLS = {KAGGLE_DOWNLOAD_DATA, KAGGLE_RUN, KAGGLE_SUBMIT}


def _spec(name: str, description: str, input_schema: dict) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema=input_schema,
        concurrency_safe=name not in _SERIAL_TOOLS,
    )


_TOOLS = {
    spec.name: (spec, handler)
    for spec, handler in (
        (
            _spec(
                KAGGLE_LIST_COMPETITIONS,
                (
                    "List Kaggle competitions, optionally filtered by a search term. Returns "
                    "each competition's slug (ref), title, deadline, reward and team count. "
                    "Use the returned ref with kaggle_get_competition to inspect one competition."
                ),
                _schema({"search": "string", "page": "integer", "sort_by": "string"}),
            ),
            _list_competitions,
        ),
        (
            _spec(
                KAGGLE_GET_COMPETITION,
                (
                    "Get one Kaggle competition's overview: title, description, evaluation "
                    "metric, tags, category, organization, team/kernel counts, daily "
                    "submission limit, and its data files. Pass the competition slug (ref) "
                    "as returned by kaggle_list_competitions."
                ),
                _schema({"competition": "string"}, "competition"),
            ),
            _get_competition,
        ),
        (
            _spec(
                KAGGLE_LIST_NOTEBOOKS,
                (
                    "List public Kaggle notebooks for a competition, sorted by votes, to find "
                    "current SOTA approaches. Returns each notebook's title, author, votes and "
                    "url. Use this to gather evidence before implementing a solution."
                ),
                _COMPETITION_LIST_SCHEMA,
            ),
            _list_notebooks,
        ),
        (
            _spec(
                KAGGLE_GET_NOTEBOOK,
                (
                    "Read a public Kaggle notebook's full source (code + markdown). Pass the "
                    "notebook ref or url returned by kaggle_list_notebooks. Returns the source "
                    "text; it is truncated to the first "
                    f"{MAX_NOTEBOOK_SOURCE_CHARS} characters."
                ),
                _schema({"notebook": "string"}, "notebook"),
            ),
            _get_notebook,
        ),
        (
            _spec(
                KAGGLE_LIST_DISCUSSIONS,
                (
                    "List public Kaggle competition discussion threads, sorted by hotness, "
                    "to find community pitfalls, failed attempts and known tricks. Returns "
                    "each thread's ref, title, author, votes, comment count and url. Use "
                    "this to gather qualitative evidence before forming hypotheses."
                ),
                _COMPETITION_LIST_SCHEMA,
            ),
            _list_discussions,
        ),
        (
            _spec(
                KAGGLE_GET_DISCUSSION,
                (
                    "Read a public Kaggle discussion thread's title, body and comments. "
                    "Pass the discussion ref or url returned by kaggle_list_discussions. "
                    "Returns the text; it is truncated to the first "
                    f"{MAX_DISCUSSION_CHARS} characters."
                ),
                _schema({"discussion": "string"}, "discussion"),
            ),
            _get_discussion,
        ),
        (
            _spec(
                KAGGLE_DOWNLOAD_DATA,
                (
                    "Download and extract a Kaggle competition's data files into the workspace, "
                    "unless the supervisor disabled local download. Returns the extracted local "
                    "file paths and a data manifest reference."
                ),
                _schema(
                    {"competition": "string", "download_subdir": "string"},
                    "competition",
                ),
            ),
            _download_data,
        ),
        (
            _spec(
                KAGGLE_RUN,
                (
                    "Fetch everything needed to start a Kaggle competition in one call: "
                    "competition metadata, downloaded data file paths (unless the supervisor "
                    "disabled local download), a data manifest reference, and top public "
                    "notebooks as evidence. Run EDA and build your baseline yourself afterward "
                    "using your own workspace and shell tools, exactly as for a local dataset."
                ),
                _schema(
                    {
                        "competition": "string",
                        "download_subdir": "string",
                        "max_notebooks": "integer",
                    },
                    "competition",
                ),
            ),
            _run,
        ),
        (
            _spec(
                KAGGLE_SUBMIT,
                (
                    "Submit a local predictions CSV to a Kaggle competition. Pass the "
                    "competition slug and the absolute path of the submission file (usually "
                    "the final predictions CSV produced by VALIDATE)."
                ),
                _schema(
                    {"competition": "string", "file_path": "string"},
                    "competition",
                    "file_path",
                ),
            ),
            _submit,
        ),
    )
}

KAGGLE_TOOL_NAMES = tuple(_TOOLS)


class KaggleTool(StackTool):
    """Bind one registered Kaggle operation to a shared runtime stack."""

    def __init__(self, stack: Any, name: str) -> None:
        super().__init__(stack)
        self.name = name

    @property
    def spec(self) -> ToolSpec:
        """Return the immutable contract for this named operation."""
        return _TOOLS[self.name][0]

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """Dispatch input to the registered operation handler."""
        return await _TOOLS[self.name][1](self.stack, input)


_EVIDENCE_TOOLS = (
    KAGGLE_LIST_NOTEBOOKS,
    KAGGLE_GET_NOTEBOOK,
    KAGGLE_LIST_DISCUSSIONS,
    KAGGLE_GET_DISCUSSION,
)

AGENT_KAGGLE_TOOLS: dict[str, tuple[str, ...]] = {
    "evaluator": (KAGGLE_GET_COMPETITION, KAGGLE_DOWNLOAD_DATA),
    "prepare": (KAGGLE_RUN,),
    "ideator": _EVIDENCE_TOOLS,
    "plan": _EVIDENCE_TOOLS,
    "kaggle_handoff": _EVIDENCE_TOOLS,
    "general": KAGGLE_TOOL_NAMES,
}


def build_kaggle_tools(
    stack: KaggleStack, names: tuple[str, ...] | None = None
) -> ToolRegistry:
    """Register the requested Kaggle operations against one shared stack."""
    tools = ToolRegistry()
    for name in names or KAGGLE_TOOL_NAMES:
        tools.register(KaggleTool(stack, name))
    return tools

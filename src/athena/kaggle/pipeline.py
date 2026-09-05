"""Fetch the Kaggle inputs consumed by Athena's PREPARE workflow."""

import json
import time
from typing import TYPE_CHECKING

from athena.core.workspace import resolve_workspace_path
from athena.kaggle.client import author_name, pick_first
from athena.kaggle.schemas import (
    Competition,
    KaggleRunReport,
    KaggleRunRequest,
    NotebookSummary,
)

if TYPE_CHECKING:
    from athena.kaggle.tool import KaggleStack


async def run_kaggle(
    stack: "KaggleStack", request: KaggleRunRequest
) -> KaggleRunReport:
    """Fetch competition metadata, optional data, and notebook evidence."""
    started = time.monotonic()
    report = KaggleRunReport(competition_ref=request.competition)

    # Connect first; later stages have no meaningful input without metadata.
    try:
        raw_competition = await stack.client.get_competition(request.competition)
    except Exception as error:  # connect failure -> empty report
        report.warnings.append(_failure("connect", error))
        raw_competition = {}
    if raw_competition:
        report.competition = _competition_from_raw(request.competition, raw_competition)
    elif not report.warnings:
        report.warnings.append("connect returned an empty competition object")

    # Download and search degrade independently after a successful connection.
    if report.competition is not None and request.download_data:
        subdir = request.download_subdir or report.competition.ref or "kaggle-data"
        target = resolve_workspace_path(stack.download_root, subdir)
        try:
            paths = await stack.client.download_competition(
                report.competition.ref, target
            )
        except Exception as error:  # download failure -> metadata-only report
            report.warnings.append(_failure("download", error))
        else:
            report.data_manifest_ref = await stack.artifacts.put_text(
                json.dumps(
                    [
                        {"name": path.name, "size": path.stat().st_size}
                        for path in paths
                    ],
                    ensure_ascii=False,
                )
            )
            report.downloaded_files = [str(path) for path in paths]
            if not paths:
                report.warnings.append("download produced no files")

    if report.competition is not None and request.max_notebooks > 0:
        try:
            raw_notebooks = await stack.client.list_notebooks(
                request.competition, page=1, sort_by="hotness"
            )
        except Exception as error:  # search failure -> omit notebook evidence
            report.warnings.append(_failure("notebook search", error))
        else:
            report.notebooks = _notebooks_from_raw(
                raw_notebooks[: request.max_notebooks]
            )

    report.http_requests = stack.client.http_request_count
    report.total_seconds = round(time.monotonic() - started, 3)
    report.status = "complete" if report.competition is not None else "empty"
    return report


def _failure(stage: str, error: Exception) -> str:
    return f"{stage} failed: {type(error).__name__}: {error}"


def _competition_from_raw(ref: str, raw: dict) -> Competition:
    return Competition(
        ref=ref,
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        evaluation_metric=pick_first(raw, "evaluationMetric", "evaluation_metric"),
        reward=raw.get("reward", ""),
        deadline=raw.get("deadline", ""),
        category=raw.get("category", ""),
        organization_name=pick_first(raw, "organizationName", "organization_name"),
        url=raw.get("url", ""),
    )


def _notebooks_from_raw(raw: list[dict]) -> list[NotebookSummary]:
    notebooks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref", "")
        notebooks.append(
            NotebookSummary(
                ref=ref,
                title=item.get("title", ""),
                author=author_name(item.get("author", "")),
                total_votes=int(
                    pick_first(item, "totalVotes", "total_votes", default=0)
                ),
                language=item.get("language", ""),
                version=str(
                    pick_first(
                        item,
                        "currentVersion",
                        "current_version",
                        "version",
                        "versionNumber",
                        default="",
                    )
                ),
                url=item.get("url", "")
                or (f"https://www.kaggle.com/code/{ref}" if ref else ""),
            )
        )
    return notebooks

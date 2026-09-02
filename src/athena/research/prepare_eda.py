"""EDA workspace preparation and graceful report fallback."""

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from athena.agents.prepare_agent import (
    PREPARE_EDA_AGENT_ID,
    PREPARE_EDA_AGENT_TYPE,
    register_prepare_eda_agent,
)
from athena.research.eda_todo import run_eda_todos

logger = logging.getLogger(__name__)

HandoffFn = Callable[..., Awaitable[str]]
PLACEHOLDER_MARK = "EDA generation failed or was skipped."


def write_missing_report_placeholders(workspace: Path) -> None:
    """Create placeholders only for missing reports named by EDA_TODO.md."""
    todo_path = workspace / "EDA_TODO.md"
    if not todo_path.is_file():
        return
    pattern = re.compile(r"^\- \[[ x]\]\s+.+?\s*->\s*([^\s#]+)")
    for line in todo_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match is None:
            continue
        name = match.group(1)
        target = workspace / name
        if name not in {"EDA_INDEX.md", "EDA_HANDOFF.md"} and not target.exists():
            target.write_text(f"# {name}\n\n{PLACEHOLDER_MARK}\n", encoding="utf-8")


def usable_eda_reports(workspace: Path) -> list[Path]:
    """Return complete EDA reports, excluding generated placeholders."""
    usable: list[Path] = []
    for report in sorted(workspace.glob("EDA_REPORT_*.md")):
        try:
            body = report.read_text(encoding="utf-8")
        except OSError:
            # A concurrently missing report is simply unavailable for aggregation.
            continue
        if PLACEHOLDER_MARK not in body:
            usable.append(report)
    return usable


def write_fallback_eda(workspace: Path) -> None:
    """Write the minimal EDA files required by the baseline stage."""
    workspace.mkdir(parents=True, exist_ok=True)
    index = workspace / "EDA_INDEX.md"
    handoff = workspace / "EDA_HANDOFF.md"
    if not index.exists():
        index.write_text(
            "# EDA Index\n\nEDA generation failed; see logs.\n", encoding="utf-8"
        )
    if not handoff.exists():
        handoff.write_text(
            "# EDA Handoff\n\nBaseline should explore the raw data.\n",
            encoding="utf-8",
        )
    write_missing_report_placeholders(workspace)


async def prepare_workspace(runtime: Any) -> Any:
    """Create the EDA worktree and persist its project-relative directory."""
    # Create an isolated worktree for all PREPARE-authored files.
    await runtime.publish_output(
        source="supervisor", channel="text", text="PREPARE: initializing EDA workspace."
    )
    base_commit = await runtime.git.init()
    workspace = await runtime.git.create(base_commit, "athena/prepare", name="eda")
    # Persist only the project-relative path used by later phases.
    runtime.state.eda_dir = str(
        Path(workspace.path).resolve().relative_to(runtime.root.resolve())
    )
    runtime.state.save(runtime.state_path)
    await runtime.publish_output(
        source="supervisor",
        channel="text",
        text=f"PREPARE: EDA workspace ready at {Path(workspace.path).resolve()}.",
    )
    return workspace


def _register_eda_agent(runtime: Any, workspace: Path) -> None:
    """Register the EDA agent once for its workspace."""
    if runtime.registry.contains(PREPARE_EDA_AGENT_TYPE):
        return
    register_prepare_eda_agent(
        runtime.registry,
        provider=runtime.provider,
        artifacts=runtime.store,
        workspace=workspace,
        runtime=runtime.execution,
        extra_tools=runtime.kaggle_tools("prepare"),
    )


async def _collect_reports(
    runtime: Any, workspace: Path, task: str, handoff_agent: HandoffFn
) -> list[Path]:
    """Generate the EDA todo list and execute its report tasks."""
    # Ask the coordinator to define reports, then run them independently.
    _register_eda_agent(runtime, workspace)
    await handoff_agent(
        agent_id=PREPARE_EDA_AGENT_ID,
        agent_type=PREPARE_EDA_AGENT_TYPE,
        workspace=str(workspace),
        output_file="EDA_TODO.md",
        content=task,
    )
    failed = await run_eda_todos(
        agents=runtime.agents,
        store=runtime.store,
        workspace=workspace,
        project_event=lambda aid, kind, ref, data: runtime.events.project_agent_event(
            aid, kind, ref, data
        ),
    )
    # Preserve successful reports when only a subset of todo items fails.
    if failed:
        write_missing_report_placeholders(workspace)
        await runtime.publish_output(
            source="supervisor",
            channel="error",
            text=f"EDA tasks failed: {failed}; continuing with complete reports.",
        )
    return usable_eda_reports(workspace)


async def _aggregate_reports(
    runtime: Any, workspace: Path, handoff_agent: HandoffFn
) -> None:
    """Aggregate usable EDA reports and publish their paths."""
    # Build the stable index and handoff consumed by baseline ideation.
    await handoff_agent(
        agent_id=PREPARE_EDA_AGENT_ID,
        agent_type=PREPARE_EDA_AGENT_TYPE,
        workspace=str(workspace),
        output_file="EDA_INDEX.md",
        content=(
            "Finalize EDA: read all EDA_REPORT_*.md and write "
            "EDA_INDEX.md and EDA_HANDOFF.md."
        ),
        reap_after=True,
    )
    if (
        not (workspace / "EDA_INDEX.md").is_file()
        or not (workspace / "EDA_HANDOFF.md").is_file()
    ):
        write_fallback_eda(workspace)
    # Surface every report path for operators and GUI consumers.
    for report in sorted(workspace.glob("EDA_REPORT_*.md")):
        await runtime.publish_output(
            source="supervisor", channel="text", text=f"EDA report: {report.resolve()}"
        )


async def prepare_eda(
    runtime: Any, workspace: Any, handoff_agent: HandoffFn, task: str
) -> bool:
    """Produce EDA handoff files, falling back when the agent boundary fails."""
    root = Path(workspace.path)
    try:
        reports = await _collect_reports(runtime, root, task, handoff_agent)
        if reports:
            await _aggregate_reports(runtime, root, handoff_agent)
            return True
        write_fallback_eda(root)
        return False
    except Exception as error:
        # Provider, agent runtime, and user handoff errors all degrade to raw-data EDA.
        logger.exception("EDA orchestration failed")
        await runtime.publish_output(
            source="supervisor",
            channel="error",
            text=f"EDA failed ({error}); using fallback files.",
        )
        write_fallback_eda(root)
        try:
            await runtime.agents.reap(PREPARE_EDA_AGENT_ID)
        except Exception:
            # Cleanup must not replace the original agent-boundary failure.
            logger.debug("EDA agent cleanup failed", exc_info=True)
        return False

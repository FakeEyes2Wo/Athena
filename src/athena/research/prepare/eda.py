"""Prepare the EDA workspace, reports, and fallback handoff files."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.agents.ideator_agent import HandoffResult
from athena.agents.prepare_agent import (
    EDA_WORKER_AGENT_TYPE,
    PREPARE_EDA_AGENT_ID,
    PREPARE_EDA_AGENT_TYPE,
    register_prepare_eda_agent,
)
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactStore
from athena.research.supervisor.events import wait_run_events
from athena.research.supervisor.experiment import load_agent_result
from athena.research.turns.common import AGENT_TURN_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

HandoffFn = Callable[..., Awaitable[str]]
PublishEvent = Callable[[str, str, str, dict | None], Awaitable[None] | None]
Todo = tuple[int, str, str]
PLACEHOLDER_MARK = "EDA generation failed or was skipped."
_STAGE_RE = re.compile(r"^##\s+.+?\(parallel:\s*(true|false)\)\s*$")
_TODO_RE = re.compile(r"^- \[ \]\s+(.+?)(?:\s*->\s*([^\s#]+))?\s*$")
_MIN_REPORT_BYTES = 200


@dataclass(frozen=True, slots=True)
class EdaTodoOptions:
    """Scheduling policy for one EDA todo run."""

    todo_file: str = "EDA_TODO.md"
    parent_id: str = PREPARE_EDA_AGENT_ID
    max_workers: int = 3
    retries: int = 2
    project_event: PublishEvent | None = None


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
    failed = await EdaTodoRunner(runtime.agents, runtime.store, workspace).run(
        EdaTodoOptions(
            project_event=lambda aid, kind, ref, data: runtime.events.project_agent_event(
                aid, kind, ref, data
            )
        )
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
        # Provider, agent runtime, and user handoff errors degrade to raw-data EDA.
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


def _is_handoff_todo(text: str, output_file: str) -> bool:
    """Identify TODO entries reserved for the final EDA handoff."""
    if output_file.upper() in {"EDA_INDEX.MD", "EDA_HANDOFF.MD"}:
        return True
    lowered = text.lower()
    return "index & handoff" in lowered or "index and handoff" in lowered


def _parse(lines: list[str]) -> list[tuple[bool, list[Todo]]]:
    """Parse staged Markdown TODO entries into executable work items."""
    stages: list[tuple[bool, list[Todo]]] = []
    for index, line in enumerate(lines):
        stage = _STAGE_RE.match(line.strip())
        if stage:
            stages.append((stage.group(1) == "true", []))
            continue
        todo = _TODO_RE.match(line.strip())
        if todo and stages:
            text = todo.group(1).strip()
            output_file = todo.group(2) or "EDA_REPORT.md"
            if not _is_handoff_todo(text, output_file):
                stages[-1][1].append((index, text, output_file))
    return stages


@dataclass(slots=True)
class EdaTodoRunner:
    """Execute staged EDA work with one shared resource bundle."""

    agents: AgentRuntime
    store: ArtifactStore
    workspace: Path

    async def run(self, options: EdaTodoOptions | None = None) -> list[str]:
        """Execute pending TODOs and return failed task descriptions."""
        options = options or EdaTodoOptions()
        todo_path = self.workspace / options.todo_file
        if not todo_path.is_file():
            return [options.todo_file]
        lines = todo_path.read_text(encoding="utf-8").splitlines()
        failed: list[str] = []
        for stage in _parse(lines):
            failed.extend(await self._run_stage(stage, lines, options))
        todo_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        return failed

    async def _run_stage(
        self,
        stage: tuple[bool, list[Todo]],
        lines: list[str],
        options: EdaTodoOptions,
    ) -> list[str]:
        """Run one staged TODO group and update successful checkboxes."""
        parallel, todos = stage
        failed: list[str] = []
        batch_size = options.max_workers if parallel else 1
        for start in range(0, len(todos), batch_size):
            batch = todos[start : start + batch_size]
            results = await asyncio.gather(
                *(self._run_one(todo, options) for todo in batch)
            )
            for (line_index, text, _), ok in zip(batch, results):
                if ok:
                    lines[line_index] = lines[line_index].replace("- [ ]", "- [x]", 1)
                else:
                    failed.append(text)
        return failed

    async def _run_one(self, todo: Todo, options: EdaTodoOptions) -> bool:
        """Run one EDA worker and verify that it wrote its assigned report."""
        _, text, output_file = todo
        existing = self.workspace / output_file
        if existing.is_file() and existing.stat().st_size > _MIN_REPORT_BYTES:
            return True
        for attempt in range(options.retries + 1):
            agent_id: str | None = None
            try:
                content = (
                    "Write exactly one EDA report file.\n\n"
                    f"Assigned output file: {output_file}\n"
                    f"Todo: {text}\n"
                    f"Workspace: {self.workspace}\n\n"
                    "Finish quickly and use at most 8 tool calls. Use only installed "
                    "dependencies; do not install packages with pip, conda, or uv. "
                    "Keep the report focused and do not exhaustively enumerate the "
                    "dataset. Do not write EDA_INDEX.md, EDA_HANDOFF.md, EDA_TODO.md, "
                    "or any file other than the assigned output file."
                )
                agent_id, run_id = await self.agents.spawn(
                    options.parent_id,
                    EDA_WORKER_AGENT_TYPE,
                    {
                        "content": content,
                        "todo_line": text,
                        "output_file": output_file,
                        "workspace": str(self.workspace),
                    },
                    name=f"eda-{output_file}",
                )

                async def publish(
                    kind: str,
                    ref: str,
                    data: dict | None = None,
                    *,
                    worker_id: str = agent_id,
                ) -> None:
                    """Forward a worker event to the runtime event bus."""
                    if options.project_event is not None:
                        await options.project_event(worker_id, kind, ref, data)

                summary = await asyncio.wait_for(
                    wait_run_events(self.agents, run_id, publish),
                    timeout=AGENT_TURN_TIMEOUT_SECONDS,
                )
                if await load_agent_result(summary, self.store, HandoffResult) is None:
                    raise RuntimeError(f"{output_file} returned no result")
                if not (self.workspace / output_file).is_file():
                    raise RuntimeError(f"{output_file} was not written")
                return True
            except TimeoutError:
                return False
            except Exception:  # noqa: BLE001 - worker retry boundary
                if attempt >= options.retries:
                    return False
                await asyncio.sleep(0.2)
            finally:
                await self._reap(agent_id)
        return False

    async def _reap(self, agent_id: str | None) -> None:
        """Release one finished worker without masking its outcome."""
        if agent_id is None:
            return
        try:
            await self.agents.reap(agent_id)
        except Exception:  # noqa: BLE001,S110 - cleanup must not mask task outcome
            pass

"""Run a Markdown checkbox todo list by spawning worker subagents.

The todo file uses stage headers and GitHub checkboxes::

    ## Stage 1: Overview (parallel: false)
    - [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md

    ## Stage 2: Profiles (parallel: true)
    - [ ] 01 Quality -> EDA_REPORT_01_DATA_QUALITY.md

``parallel: false`` stages run sequentially; ``parallel: true`` stages run up
to ``max_workers`` workers concurrently. Existing reports are reused, while
failed items are left unchecked for the deterministic fallback path.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from athena.agents.ideator_agent import HandoffResult
from athena.agents.prepare_agent import EDA_WORKER_AGENT_TYPE, PREPARE_EDA_AGENT_ID
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactStore
from athena.research.supervisor.experiment import load_agent_result
from athena.research.supervisor.plans import wait_run_events

_STAGE_RE = re.compile(r"^##\s+.+?\(parallel:\s*(true|false)\)\s*$")
_TODO_RE = re.compile(r"^- \[ \]\s+(.+?)(?:\s*->\s*([^\s#]+))?\s*$")
PublishEvent = Callable[[str, str, str, dict | None], Awaitable[None]]
Todo = tuple[int, str, str]  # (line_index, text, output_file)
EDA_WORKER_TIMEOUT_SECONDS = 180
EDA_WORKER_RETRIES = 0
_MIN_REPORT_BYTES = 64


def _usable_report(workspace: Path, output_file: str) -> bool:
    """Return whether an existing report is safe to reuse after interruption."""
    try:
        root = workspace.resolve()
        path = (workspace / output_file).resolve()
        path.relative_to(root)
        if not path.is_file() or path.stat().st_size < _MIN_REPORT_BYTES:
            return False
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return False
    stripped = text.lstrip()
    return stripped.startswith("#") and (
        "eda:" in text or "EDA generation failed or was skipped." in text
    )


async def _cancel_worker(agents: AgentRuntime, agent_id: str, run_id: str) -> None:
    """Stop the exact timed-out run so it cannot keep spending API quota."""
    cancel_run = getattr(agents, "cancel_run", None)
    try:
        if callable(cancel_run):
            await cancel_run(agent_id, run_id, "eda_worker_timeout")
        else:
            await agents.interrupt(agent_id, "eda_worker_timeout")
    except Exception:
        # The run may already have reached a terminal state.
        pass

# EDA_INDEX/EDA_HANDOFF are written by the PREPARE_EDA orchestrator in its
# finalize turn, not by an EDA worker (which is forbidden to write them).
def _is_handoff_todo(text: str, output_file: str) -> bool:
    if output_file.upper() in {"EDA_INDEX.MD", "EDA_HANDOFF.MD"}:
        return True
    lowered = text.lower()
    return "index & handoff" in lowered or "index and handoff" in lowered


def _parse(lines: list[str]) -> list[tuple[bool, list[Todo]]]:
    """Parse markdown lines into [(parallel, [(line_index, text, output_file)])]."""
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
            if _is_handoff_todo(text, output_file):
                continue
            stages[-1][1].append((index, text, output_file))
    return stages


async def _run_one(
    todo: Todo,
    *,
    agents: AgentRuntime,
    store: ArtifactStore,
    workspace: Path,
    parent_id: str,
    retries: int,
    project_event: PublishEvent | None,
    timeout_seconds: float,
) -> bool:
    """Spawn one worker, wait for it, and verify its output file."""
    _, text, output_file = todo
    if _usable_report(workspace, output_file):
        return True
    for attempt in range(retries + 1):
        agent_id: str | None = None
        try:
            # Prompt-driven agents read the task from ``content``. Passing only
            # structured fields left the model with an empty user turn, so it
            # started guessing which report file to write. Put the exact file
            # name in the prompt and keep the structured fields for audit.
            content = (
                f"Write exactly one EDA report file.\n\n"
                f"Assigned output file: {output_file}\n"
                f"Todo: {text}\n"
                f"Workspace: {workspace}\n\n"
                "Do not write EDA_INDEX.md, EDA_HANDOFF.md, EDA_TODO.md, or any "
                "file other than the assigned output file."
            )
            agent_id, run_id = await agents.spawn(
                parent_id,
                EDA_WORKER_AGENT_TYPE,
                {
                    "content": content,
                    "todo_line": text,
                    "output_file": output_file,
                    "workspace": str(workspace),
                },
                name=f"eda-{output_file}",
            )

            async def publish(kind: str, ref: str, data: dict | None = None) -> None:
                """Forward one worker event to the runtime event bus."""
                if project_event is not None and agent_id is not None:
                    await project_event(agent_id, kind, ref, data)

            try:
                summary = await asyncio.wait_for(
                    wait_run_events(agents, run_id, publish),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                await _cancel_worker(agents, agent_id, run_id)
                raise RuntimeError(
                    f"{output_file} timed out after {timeout_seconds:g}s"
                ) from None
            # The report is the durable contract. Accept it even if the model's
            # final JSON was malformed, avoiding a full EDA rerun for formatting.
            if _usable_report(workspace, output_file):
                return True
            if await load_agent_result(summary, store, HandoffResult) is None:
                raise RuntimeError(f"{output_file} returned no result")
            raise RuntimeError(f"{output_file} was not written or was incomplete")
        except Exception:
            if _usable_report(workspace, output_file):
                return True
            if attempt >= retries:
                return False
            await asyncio.sleep(0.2)
        finally:
            # EDA workers are one-shot subagents: reap immediately so a long
            # PREPARE phase does not accumulate threads/rollout metadata.
            if agent_id is not None:
                try:
                    await agents.reap(agent_id)
                except Exception:  # noqa: BLE001,S110 - GC must never mask task failure
                    pass
    return False


async def _run_stage(
    stage: tuple[bool, list[Todo]],
    *,
    agents: AgentRuntime,
    store: ArtifactStore,
    workspace: Path,
    lines: list[str],
    parent_id: str,
    max_workers: int,
    retries: int,
    project_event: PublishEvent | None,
    timeout_seconds: float,
) -> list[str]:
    """Run one stage, updating checkboxes in ``lines``; return failed todo text."""
    parallel, todos = stage
    failed: list[str] = []
    batch_size = max_workers if parallel else 1
    for start in range(0, len(todos), batch_size):
        batch = todos[start : start + batch_size]
        results = await asyncio.gather(
            *(
                _run_one(
                    todo,
                    agents=agents,
                    store=store,
                    workspace=workspace,
                    parent_id=parent_id,
                    retries=retries,
                    project_event=project_event,
                    timeout_seconds=timeout_seconds,
                )
                for todo in batch
            )
        )
        for (line_index, text, _), ok in zip(batch, results):
            if ok:
                lines[line_index] = lines[line_index].replace("- [ ]", "- [x]", 1)
            else:
                failed.append(text)
    return failed


async def run_eda_todos(
    *,
    agents: AgentRuntime,
    store: ArtifactStore,
    workspace: Path,
    todo_file: str = "EDA_TODO.md",
    parent_id: str = PREPARE_EDA_AGENT_ID,
    max_workers: int = 3,
    retries: int = EDA_WORKER_RETRIES,
    timeout_seconds: float = EDA_WORKER_TIMEOUT_SECONDS,
    project_event: PublishEvent | None = None,
) -> list[str]:
    """Execute pending todos by spawning worker subagents; return failed texts."""
    todo_path = workspace / todo_file
    if not todo_path.is_file():
        return [todo_file]
    lines = todo_path.read_text(encoding="utf-8").splitlines()
    failed: list[str] = []
    for stage in _parse(lines):
        failed.extend(
            await _run_stage(
                stage,
                agents=agents,
                store=store,
                workspace=workspace,
                lines=lines,
                parent_id=parent_id,
                max_workers=max_workers,
                retries=retries,
                project_event=project_event,
                timeout_seconds=timeout_seconds,
            )
        )
    todo_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return failed

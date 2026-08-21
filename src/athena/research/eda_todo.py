"""Run ``EDA_TODO.md`` by spawning ``eda_worker`` subagents.

The Markdown todo list uses GitHub checkboxes and stage headers::

    ## Stage 1: Overview (parallel: false)
    - [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md

    ## Stage 2: Independent Profiles (parallel: true)
    - [ ] 01 Data Quality -> EDA_REPORT_01_DATA_QUALITY.md

Each pending ``- [ ]`` line is executed by one ``eda_worker`` subagent.
``parallel: false`` stages run strictly in order; ``parallel: true`` stages
run up to ``max_workers`` workers concurrently. Failed items are retried and,
if still failing, left as ``- [ ]``.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.agents.ideator_agent import HandoffResult
from athena.agents.prepare_eda_agent import EDA_WORKER_AGENT_TYPE, PREPARE_EDA_AGENT_ID
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactStore
from athena.research.supervisor.experiment import load_agent_result
from athena.research.supervisor.plans import wait_run_events

_STAGE_RE = re.compile(r"^##\s+.+?\(parallel:\s*(true|false)\)\s*$")
_TODO_RE = re.compile(r"^- \[ \]\s+(.+?)(?:\s*->\s*([^\s#]+))?\s*$")

PublishEvent = Callable[[str, str, str, dict | None], Awaitable[None] | None]


@dataclass
class _Todo:
    line_index: int
    text: str
    output_file: str
    parallel: bool


@dataclass
class _Stage:
    parallel: bool
    todos: list[_Todo] = field(default_factory=list)


def _parse_todos(lines: list[str]) -> list[_Stage]:
    """Parse stage headers and pending checkbox todos from markdown lines."""
    stages: list[_Stage] = []
    current: _Stage | None = None
    for index, line in enumerate(lines):
        stage_match = _STAGE_RE.match(line.strip())
        if stage_match:
            current = _Stage(parallel=stage_match.group(1) == "true")
            stages.append(current)
            continue
        todo_match = _TODO_RE.match(line.strip())
        if todo_match and current is not None:
            text = todo_match.group(1).strip()
            output_file = todo_match.group(2) or "EDA_REPORT.md"
            current.todos.append(
                _Todo(
                    line_index=index,
                    text=text,
                    output_file=output_file,
                    parallel=current.parallel,
                )
            )
    return stages


async def _run_one_todo(
    todo: _Todo,
    *,
    agents: AgentRuntime,
    store: ArtifactStore,
    workspace: Path,
    parent_id: str,
    retries: int,
    project_event: PublishEvent | None,
) -> bool:
    """Spawn one eda_worker, wait for it, and verify its report file."""
    for attempt in range(retries + 1):
        agent_id: str | None = None
        try:
            task = {
                "todo_line": todo.text,
                "output_file": todo.output_file,
                "workspace": str(workspace),
            }
            agent_id, run_id = await agents.spawn(
                parent_id,
                EDA_WORKER_AGENT_TYPE,
                task,
                name=f"eda-{todo.output_file}",
            )

            def publish(kind: str, ref: str, data: dict | None = None) -> None:
                """Forward one worker agent event to the runtime event bus."""
                if project_event is not None and agent_id is not None:
                    project_event(agent_id, kind, ref, data)

            summary = await wait_run_events(agents, run_id, publish)
            result = await load_agent_result(summary, store, HandoffResult)
            if result is None:
                raise RuntimeError(f"{todo.output_file} worker returned no result")
            output = workspace / todo.output_file
            if not output.is_file():
                raise RuntimeError(f"{todo.output_file} was not written")
            return True
        except Exception:
            if attempt >= retries:
                return False
            await asyncio.sleep(0.2)
    return False


async def _run_stage(
    stage: _Stage,
    *,
    agents: AgentRuntime,
    store: ArtifactStore,
    workspace: Path,
    lines: list[str],
    parent_id: str,
    max_workers: int,
    retries: int,
    project_event: PublishEvent | None,
) -> list[str]:
    """Run one stage, updating checkboxes in ``lines``; return failed todo text."""
    failed: list[str] = []
    if stage.parallel:
        for start in range(0, len(stage.todos), max_workers):
            batch = stage.todos[start : start + max_workers]
            results = await asyncio.gather(
                *(
                    _run_one_todo(
                        todo,
                        agents=agents,
                        store=store,
                        workspace=workspace,
                        parent_id=parent_id,
                        retries=retries,
                        project_event=project_event,
                    )
                    for todo in batch
                )
            )
            for todo, ok in zip(batch, results):
                if ok:
                    lines[todo.line_index] = lines[todo.line_index].replace(
                        "- [ ]", "- [x]", 1
                    )
                else:
                    failed.append(todo.text)
    else:
        for todo in stage.todos:
            ok = await _run_one_todo(
                todo,
                agents=agents,
                store=store,
                workspace=workspace,
                parent_id=parent_id,
                retries=retries,
                project_event=project_event,
            )
            if ok:
                lines[todo.line_index] = lines[todo.line_index].replace(
                    "- [ ]", "- [x]", 1
                )
            else:
                failed.append(todo.text)
    return failed


async def run_eda_todos(
    *,
    agents: AgentRuntime,
    store: ArtifactStore,
    workspace: Path,
    todo_file: str = "EDA_TODO.md",
    parent_id: str = PREPARE_EDA_AGENT_ID,
    max_workers: int = 3,
    retries: int = 2,
    project_event: PublishEvent | None = None,
) -> list[str]:
    """Execute pending EDA todos by spawning eda_worker subagents.

    Returns the text of todos that remain unfinished after retries.
    """
    todo_path = workspace / todo_file
    if not todo_path.is_file():
        return [todo_file]
    lines = todo_path.read_text(encoding="utf-8").splitlines()
    stages = _parse_todos(lines)
    failed: list[str] = []
    for stage in stages:
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
            )
        )
    todo_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return failed

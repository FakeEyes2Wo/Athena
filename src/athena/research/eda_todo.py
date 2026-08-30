"""Run a Markdown checkbox todo list by spawning worker subagents.

The todo file uses stage headers and GitHub checkboxes::

    ## Stage 1: Overview (parallel: false)
    - [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md

    ## Stage 2: Profiles (parallel: true)
    - [ ] 01 Quality -> EDA_REPORT_01_DATA_QUALITY.md

``parallel: false`` stages run sequentially; ``parallel: true`` stages run up
to ``max_workers`` workers concurrently. Failed items are retried and left
unchecked if they still fail.
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
PublishEvent = Callable[[str, str, str, dict | None], Awaitable[None] | None]
Todo = tuple[int, str, str]  # (line_index, text, output_file)


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
) -> bool:
    """Spawn one worker, wait for it, and verify its output file."""
    _, text, output_file = todo
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
                """Forward one worker event to the runtime event bus.

                必须是协程：``wait_run_events`` 无条件 ``await publish(...)``，同步版
                返回 None，worker 的第一条 journal 事件就抛 "object NoneType can't be
                used in 'await' expression"；异常被下面的 except 吞掉并重试，重试同样
                失败，整张 todo 表一条也完不成。``project_event`` 按契约可能返回协程
                也可能返回 None，两种都要接住。
                """
                if project_event is None or agent_id is None:
                    return
                projection = project_event(agent_id, kind, ref, data)
                if projection is not None:
                    await projection

            summary = await wait_run_events(agents, run_id, publish)
            if await load_agent_result(summary, store, HandoffResult) is None:
                raise RuntimeError(f"{output_file} returned no result")
            if not (workspace / output_file).is_file():
                raise RuntimeError(f"{output_file} was not written")
            return True
        except Exception:
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
    retries: int = 2,
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
            )
        )
    todo_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return failed

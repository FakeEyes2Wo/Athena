"""Baseline design and trusted PREPARE execution."""

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    register_ideator_agent,
)
from athena.agents.prepare_agent import register_prepare_agent
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.research.supervisor.prepare import PrepareResult, run_prepare_plan

logger = logging.getLogger(__name__)

HandoffFn = Callable[..., Awaitable[str]]


def directory_candidate_task(task: str) -> str:
    """Add the directory-data evaluation split contract to a candidate task."""
    return (
        f"{task}\n\nFor directory data, read ATHENA_EVALUATION_SPLIT from "
        "os.environ. It is 'search' during SEARCH and 'final' during VALIDATE. "
        "Use it to select the matching deterministic partition; never hardcode "
        "one split or let groups cross partitions."
    )


def _register_ideator(runtime: Any, workspace: Path) -> None:
    """Register the baseline ideator once for the EDA workspace."""
    profile = BASELINE_IDEATOR_PROFILE
    if runtime.registry.contains(profile.agent_type):
        return
    register_ideator_agent(
        runtime.registry,
        provider=runtime.provider,
        artifacts=runtime.store,
        workspace=workspace,
        runtime=runtime.execution,
        extra_tools=runtime.ideator_tools(),
        gated=True,
        profile=profile,
    )


async def prepare_baseline_design(
    runtime: Any,
    workspace: Any,
    task: str,
    eda_ready: bool,
    handoff_agent: HandoffFn,
) -> None:
    """Ask the baseline ideator to turn EDA findings into a design."""
    if not eda_ready:
        await runtime.publish_output(
            source="supervisor",
            channel="text",
            text="PREPARE: skipping baseline design because EDA is unavailable.",
        )
        return
    try:
        root = Path(workspace.path)
        _register_ideator(runtime, root)
        await handoff_agent(
            agent_id=BASELINE_IDEATOR_PROFILE.agent_type,
            agent_type=BASELINE_IDEATOR_PROFILE.agent_type,
            workspace=str(root),
            output_file="BASELINE_DESIGN.md",
            content=f"{task}\n\nRead EDA_HANDOFF.md and write BASELINE_DESIGN.md.",
            reap_after=True,
        )
    except Exception as error:
        # Ideator/provider failures are optional; PREPARE can implement task-only.
        logger.exception("Baseline design failed")
        await runtime.publish_output(
            source="supervisor",
            channel="error",
            text=f"Baseline design failed ({error}); using the task only.",
        )


def _register_prepare_agent(runtime: Any, workspace: Path) -> None:
    """Register the baseline implementation agent once."""
    if runtime.registry.contains("prepare"):
        return
    register_prepare_agent(
        runtime.registry,
        provider=runtime.provider,
        artifacts=runtime.store,
        workspace=workspace,
        runtime=runtime.execution,
        extra_tools=runtime.kaggle_tools("prepare"),
    )


async def run_baseline(
    runtime: Any,
    workspace: Any,
    evaluator_ref: Any,
    task: str,
    predict_features: Path | None,
) -> PrepareResult:
    """Implement and score the trusted baseline through the frozen evaluator."""
    # Register the implementation agent and freeze the current research tree.
    await runtime.publish_output(
        source="supervisor", channel="text", text="PREPARE: implementing baseline."
    )
    _register_prepare_agent(runtime, Path(workspace.path))
    tree_ref = await runtime.store.put_text(
        json.dumps(runtime.tree.to_dict(), ensure_ascii=False, sort_keys=True)
    )
    # Delegate execution and trusted scoring to the supervisor plan.
    return await run_prepare_plan(
        agents=runtime.agents,
        evaluator=runtime.evaluator,
        git=runtime.git,
        workspace=workspace,
        execution=runtime.execution,
        store=runtime.store,
        evaluator_ref=evaluator_ref,
        tree_ref=tree_ref,
        task=task,
        max_turns=MAX_PLAN_TURNS,
        publish=lambda kind, ref, data: runtime.events.project_agent_event(
            "prepare", kind, ref, data
        ),
        predict_features=predict_features,
    )

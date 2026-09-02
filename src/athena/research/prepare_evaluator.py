"""SEARCH and FINAL evaluator preparation."""

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.agents.prepare_agent import EVALUATOR_AGENT_TYPE, register_evaluator_agent
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.core.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    InvalidArtifactRefError,
)
from athena.research.supervisor.evaluator_plan import (
    EVALUATOR_AGENT_ID,
    EVALUATOR_PLAN_ID,
    FINAL_EVALUATOR_AGENT_ID,
    FINAL_EVALUATOR_PLAN_ID,
    run_evaluator_plan,
)

ROW_ID_DECLARATION = (
    "Row ID algorithm: for each logical sample, sort its member source files by "
    "POSIX dataset-relative path; __athena_row_id is the first path. This "
    "algorithm is identical for SEARCH and FINAL and never depends on split "
    "assignment or labels."
)


@dataclass(frozen=True)
class EvaluatorJob:
    """Describe one evaluator workspace and supervisor plan."""

    directory: str
    agent_id: str
    plan_id: str
    task: str
    event_label: str


@dataclass(frozen=True)
class EvaluatorBundle:
    """Frozen evaluator references for SEARCH and FINAL."""

    search_ref: Any
    final_ref: Any


DIRECTORY_EVALUATOR_CONTRACT = (
    "The dataset has no platform CSV split. Create deterministic, group-disjoint "
    "SEARCH and FINAL partitions. Normalize each group id, order groups by its "
    "SHA-256 digest, set cut = max(1, group_count // 5), assign the first cut "
    "groups to FINAL and the next cut groups to SEARCH. Never place one group in "
    "both partitions. Write labels.csv with the exact columns "
    "__athena_row_id,label. The SEARCH and FINAL evaluators must derive row ids "
    "with exactly the same algorithm, independent of partition membership and "
    "labels. Each evaluator's HANDOFF.md must contain this exact sentence, "
    f"verbatim: {ROW_ID_DECLARATION} SEARCH and FINAL predictions CSV files "
    "must have exactly these columns, in this order: "
    "__athena_row_id,prediction. Each HANDOFF.md must contain these exact "
    "lines verbatim:\nprediction_id_column: __athena_row_id\n"
    "prediction_column: prediction\nHANDOFF.md must also declare label "
    "mapping and sample count. "
    "evaluate.py must reject missing, duplicate, or unexpected ids."
)


def label_row_ids(labels_csv: Path) -> set[str]:
    """Read stable row ids from an evaluator labels CSV."""
    try:
        with labels_csv.open(encoding="utf-8-sig", newline="") as handle:
            return {
                row["__athena_row_id"]
                for row in csv.DictReader(handle)
                if row.get("__athena_row_id")
            }
    except (OSError, ValueError, KeyError):
        # Evaluator freeze owns malformed or missing label errors.
        return set()


def assert_evaluator_splits_are_disjoint(
    search_labels: Path, final_labels: Path
) -> None:
    """Reject overlap between SEARCH and FINAL evaluator rows."""
    search_ids = label_row_ids(search_labels)
    final_ids = label_row_ids(final_labels)
    if not search_ids or not final_ids:
        return
    overlap = search_ids & final_ids
    if overlap:
        raise RuntimeError(
            "SEARCH and FINAL evaluators score overlapping rows "
            f"({len(overlap)} of {len(final_ids)} final rows). The held-out split "
            f"is not held out: {search_labels} vs {final_labels}"
        )


async def reusable_ref(runtime: Any, ref: Any) -> Any:
    """Return a readable frozen artifact reference, or ``None``."""
    if ref is None:
        return None
    try:
        await runtime.store.get_text(ref)
    except (
        ArtifactNotFoundError,
        ArtifactIntegrityError,
        InvalidArtifactRefError,
    ):
        # A stale checkpoint must be rebuilt by the evaluator agent.
        return None
    return ref


def evaluator_tasks(runtime: Any, task: str) -> tuple[str, str]:
    """Render SEARCH and FINAL tasks for CSV or directory data."""
    final_labels = runtime.workspaces_root / "data_split" / "final_labels.csv"
    if final_labels.is_file():
        final_rule = (
            "Take the final labels from final_labels.csv in the platform's "
            "data_split directory, and copy them into your workspace."
        )
        return task, final_rule
    search = (
        f"{task}\n\n{DIRECTORY_EVALUATOR_CONTRACT}\n\n"
        "You are building the SEARCH evaluator. Use only the SEARCH partition. "
        "Do not read, copy, derive, or disclose FINAL labels."
    )
    final = f"{DIRECTORY_EVALUATOR_CONTRACT}\n\nUse only the FINAL partition."
    return search, final


def final_evaluator_task(task: str, workspace: Path, data_rule: str) -> str:
    """Render the isolation rules for the hidden FINAL evaluator."""
    return (
        f"{task}\n\nYou are building the FINAL evaluator. Use a held-out split "
        "disjoint from SEARCH. This evaluator is hidden from SEARCH and used only "
        f"by VALIDATE. Your empty workspace is {workspace.resolve()}. Every file "
        "must be written inside it. The sibling SEARCH evaluator is frozen: do not "
        f"read, copy, or edit it.\n{data_rule}"
    )


async def run_evaluator_agent(
    runtime: Any,
    job: EvaluatorJob,
) -> str:
    """Bind one evaluator agent to one directory and freeze its output."""
    evaluator_dir = runtime.workspaces_root / job.directory
    evaluator_dir.mkdir(parents=True, exist_ok=True)

    # Rebind because the agent factory captures its workspace at registration.
    if runtime.registry.contains(EVALUATOR_AGENT_TYPE):
        runtime.registry.unregister(EVALUATOR_AGENT_TYPE)
    register_evaluator_agent(
        runtime.registry,
        provider=runtime.provider,
        artifacts=runtime.store,
        workspace=evaluator_dir,
        runtime=runtime.execution,
        extra_tools=runtime.kaggle_tools("evaluator"),
    )

    # The supervisor plan validates and freezes the evaluator bundle.
    return await run_evaluator_plan(
        agents=runtime.agents,
        scripts=runtime.scripts,
        store=runtime.store,
        evaluator_dir=evaluator_dir,
        execution=runtime.execution,
        task=job.task,
        max_turns=MAX_PLAN_TURNS,
        publish=lambda kind, ref, data: runtime.events.project_agent_event(
            job.event_label, kind, ref, data
        ),
        ask_user=getattr(runtime, "ask_user", None),
        agent_id=job.agent_id,
        plan_id=job.plan_id,
    )


async def _search_evaluator(runtime: Any, task: str) -> Any:
    """Reuse or freeze the SEARCH evaluator."""
    # Reuse a readable checkpoint before starting another costly agent plan.
    ref = await reusable_ref(runtime, runtime.supervisor.evaluator_ref)
    if ref is not None:
        await runtime.publish_output(
            source="supervisor",
            channel="text",
            text="PREPARE: reused SEARCH evaluator.",
        )
        return ref
    await runtime.publish_output(
        source="supervisor", channel="text", text="PREPARE: building SEARCH evaluator."
    )
    # Freeze and checkpoint a new evaluator when no valid artifact remains.
    ref = await run_evaluator_agent(
        runtime,
        EvaluatorJob(
            directory="evaluator",
            agent_id=EVALUATOR_AGENT_ID,
            plan_id=EVALUATOR_PLAN_ID,
            task=task,
            event_label="evaluator",
        ),
    )
    await runtime.supervisor.checkpoint_evaluator(ref)
    return ref


async def _final_evaluator(
    runtime: Any,
    task: str,
) -> Any:
    """Reuse or freeze the hidden FINAL evaluator."""
    # Keep FINAL independently checkpointed and invisible to SEARCH.
    ref = await reusable_ref(runtime, runtime.supervisor.final_evaluator_ref)
    if ref is not None:
        return ref
    await runtime.publish_output(
        source="supervisor", channel="text", text="PREPARE: building FINAL evaluator."
    )
    ref = await run_evaluator_agent(
        runtime,
        EvaluatorJob(
            directory="final_evaluator",
            agent_id=FINAL_EVALUATOR_AGENT_ID,
            plan_id=FINAL_EVALUATOR_PLAN_ID,
            task=task,
            event_label="final_evaluator",
        ),
    )
    # Prove row isolation before making the FINAL bundle durable.
    roots = runtime.workspaces_root
    assert_evaluator_splits_are_disjoint(
        roots / "evaluator" / "labels.csv",
        roots / "final_evaluator" / "labels.csv",
    )
    await runtime.supervisor.checkpoint_final_evaluator(ref)
    return ref


async def prepare_evaluators(
    runtime: Any,
    task: str,
) -> EvaluatorBundle:
    """Freeze the visible SEARCH and hidden FINAL evaluator bundles."""
    # Build matching role prompts from one data-source contract.
    search_task, final_rule = evaluator_tasks(runtime, task)
    search_ref = await _search_evaluator(runtime, search_task)
    final_dir = runtime.workspaces_root / "final_evaluator"
    final_task = final_evaluator_task(task, final_dir, final_rule)
    # Freeze FINAL only after SEARCH so overlap can be checked immediately.
    final_ref = await _final_evaluator(runtime, final_task)
    return EvaluatorBundle(search_ref=search_ref, final_ref=final_ref)

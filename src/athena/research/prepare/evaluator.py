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
from athena.research.evaluation.spec import load_evaluator_spec
from athena.research.supervisor.evaluator_plan import (
    EVALUATOR_PLAN_ID,
    FINAL_EVALUATOR_PLAN_ID,
    run_evaluator_plan,
)


@dataclass(frozen=True)
class EvaluatorJob:
    """Describe one evaluator workspace and supervisor plan."""

    name: str
    task: str


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
    "both partitions. Choose an opaque, stable task_id and declare the complete "
    "prediction contract in metric.json: task_id, prediction_file="
    "predictions__{task_id}.csv, prediction_id_column, prediction_column, "
    "optional probability_columns, metrics_file=metrics_public_test.csv, and "
    "eval_script=eval_metrics.py. Use the dataset's actual identity and target "
    "fields; do not assume JW-SSD labels or class names. The SEARCH and FINAL "
    "evaluators must derive ids with exactly the same algorithm, independent of "
    "partition membership and labels. Identity values must not contain target "
    "names, class names, or label-derived directory segments; directory datasets "
    "should use a label-free basename or canonical sample key. A binary CSV may use "
    "__athena_row_id,label, but that is only an example; use the dataset's "
    "declared fields. Write labels.csv (or labels/ for a "
    "non-tabular task), and make HANDOFF.md repeat the machine-readable id/value "
    "declarations, label mapping, sample count, and metric-table definition. "
    "The evaluator must reject missing, duplicate, or unexpected ids. For "
    "multiclass tasks, describe the declared class list and any one-vs-rest or "
    "folded-binary TSS/HSS rows in metrics_public_test.csv; macro/micro F1 notes "
    "must match eval_metrics.py."
)


def label_row_ids(labels_csv: Path, *, id_column: str = "__athena_row_id") -> set[str]:
    """Read stable row ids from an evaluator labels CSV."""
    try:
        with labels_csv.open(encoding="utf-8-sig", newline="") as handle:
            return {
                row[id_column] for row in csv.DictReader(handle) if row.get(id_column)
            }
    except (OSError, ValueError, KeyError):
        # Evaluator freeze owns malformed or missing label errors.
        return set()


def _labels_path(root: Path) -> Path:
    """Find the canonical labels file in a new or legacy evaluator root."""
    direct = root / "labels.csv"
    if direct.is_file():
        return direct
    labels_dir = root / "labels"
    files = sorted(labels_dir.rglob("*.csv")) if labels_dir.is_dir() else []
    return files[0] if files else direct


def assert_evaluator_splits_are_disjoint(
    search_labels: Path,
    final_labels: Path,
    *,
    id_column: str = "__athena_row_id",
) -> None:
    """Reject overlap between SEARCH and FINAL evaluator rows."""
    search_ids = label_row_ids(search_labels, id_column=id_column)
    final_ids = label_row_ids(final_labels, id_column=id_column)
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
        # ``DataContract.evaluator_task`` already carries the full generic
        # contract in the platform-split path.  Keep direct legacy callers
        # unchanged while adding the contract there in production.
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
    # The agent owns the role workspace, while ``evaluate/`` is the only
    # authoritative bundle root recorded in its descriptor.  Keeping these
    # distinct lets the prompt enforce an evaluate/ subdirectory without
    # accidentally creating evaluate/evaluate/.
    agent_workspace = runtime.workspaces_root / job.name
    evaluator_dir = agent_workspace / "evaluate"
    evaluator_dir.mkdir(parents=True, exist_ok=True)

    # Rebind because the agent factory captures its workspace at registration.
    if runtime.registry.contains(EVALUATOR_AGENT_TYPE):
        runtime.registry.unregister(EVALUATOR_AGENT_TYPE)
    register_evaluator_agent(
        runtime.registry,
        provider=runtime.provider,
        artifacts=runtime.store,
        workspace=agent_workspace,
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
            job.name, kind, ref, data
        ),
        ask_user=getattr(runtime, "ask_user", None),
        agent_id=job.name,
        plan_id=job.name,
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
            name=EVALUATOR_PLAN_ID,
            task=task,
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
            name=FINAL_EVALUATOR_PLAN_ID,
            task=task,
        ),
    )
    # Prove row isolation before making the FINAL bundle durable.
    roots = runtime.workspaces_root
    search_root = roots / "evaluator" / "evaluate"
    if not search_root.is_dir():
        search_root = roots / "evaluator"
    final_root = roots / "final_evaluator" / "evaluate"
    if not final_root.is_dir():
        final_root = roots / "final_evaluator"
    search_labels = _labels_path(search_root)
    final_labels = _labels_path(final_root)
    search_spec = (
        load_evaluator_spec(search_root)
        if (search_root / "metric.json").is_file()
        else None
    )
    final_spec = (
        load_evaluator_spec(final_root)
        if (final_root / "metric.json").is_file()
        else None
    )
    id_column = "__athena_row_id"
    if search_spec is not None:
        id_column = search_spec.prediction_id_column
    if final_spec is not None and final_spec.prediction_id_column != id_column:
        raise RuntimeError(
            "SEARCH and FINAL evaluators declare different prediction id columns"
        )
    if id_column == "__athena_row_id":
        # Keep the two-argument call shape for legacy descriptor hooks.
        assert_evaluator_splits_are_disjoint(search_labels, final_labels)
    else:
        assert_evaluator_splits_are_disjoint(
            search_labels, final_labels, id_column=id_column
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
    final_dir = runtime.workspaces_root / "final_evaluator" / "evaluate"
    final_task = final_evaluator_task(task, final_dir, final_rule)
    # Freeze FINAL only after SEARCH so overlap can be checked immediately.
    final_ref = await _final_evaluator(runtime, final_task)
    return EvaluatorBundle(search_ref=search_ref, final_ref=final_ref)

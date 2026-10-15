"""SEARCH and FINAL evaluator preparation."""

import csv
import json
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
from athena.research.contracts import EvaluatorDescriptor
from athena.research.evaluation.spec import load_evaluator_spec
from athena.research.supervisor.evaluator_plan import run_evaluator_plan

EVALUATOR_PLAN_ID = "evaluator"
FINAL_EVALUATOR_PLAN_ID = "final_evaluator"


@dataclass(frozen=True)
class EvaluatorBundle:
    """Frozen evaluator references available after PREPARE.

    FINAL is deliberately absent here.  It is created only when an authorized
    VALIDATE phase has a frozen SOTA, so a SEARCH-only run never provisions or
    touches the held-out evaluator.
    """

    search_ref: Any
    final_ref: Any = None


DIRECTORY_EVALUATOR_CONTRACT = (
    "The dataset has no platform CSV split. Create deterministic, group-disjoint "
    "SEARCH and FINAL partitions. Normalize each group id, order groups by its "
    "SHA-256 digest, set cut = max(1, group_count // 5), assign the first cut "
    "groups to FINAL and the next cut groups to SEARCH. Never place one group in "
    "both partitions. Choose an opaque, stable task_id and declare the complete "
    "prediction contract in metric.json: contract_version=2, task_id, task_type, "
    "primary_metric, class_labels, prediction_file="
    "predictions__{task_id}.csv, prediction_id_column, prediction_column, "
    "optional probability_columns, metrics_file=metrics_public_test.csv, and "
    "eval_script=eval_metrics.py. Use the dataset's actual identity and target "
    "fields; do not assume JW-SSD labels or class names. For directory data, "
    "prediction_id_column must be exactly __athena_row_id in both evaluators; "
    "derive its values from the same label-free sample identity. "
    "The two evaluators must declare the same task_type, primary_metric, and "
    "complete class_labels. For classification macro-F1, always score across the "
    "full declared class_labels with zero for an absent class; never infer or "
    "shrink the label universe from one partition. Evaluators must derive ids "
    "with exactly the same algorithm, independent of "
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

PLATFORM_EVALUATOR_CONTRACT = (
    "The platform has already split the dataset into train, SEARCH, and FINAL; "
    "do not create another split. Build the complete evaluator under evaluate/ "
    "with metric.json, eval_metrics.py, labels.csv (or labels/), HANDOFF.md, "
    "and pyproject.toml. Declare contract_version=2, task_id, task_type, "
    "primary_metric, the complete class_labels for classification, "
    "prediction_file=predictions__{task_id}.csv, prediction_id_column, "
    "prediction_column, optional probability_columns, "
    "metrics_file=metrics_public_test.csv, and eval_script in metric.json. "
    "SEARCH and FINAL must use the same complete class_labels; macro-F1 must "
    "not drop classes absent from one split. Keep final labels hidden from SEARCH."
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
    """Return a frozen reference only when its descriptor and files are valid."""
    if ref is None:
        return None
    try:
        descriptor = EvaluatorDescriptor.model_validate_json(
            await runtime.store.get_text(ref)
        )
        root = Path(descriptor.dir_path)
        entrypoint = Path(descriptor.entrypoint)
        if (
            not root.is_dir()
            or entrypoint.is_absolute()
            or ".." in entrypoint.parts
            or not (root / entrypoint).is_file()
            or not (root / "README.md").is_file()
        ):
            return None
        if (root / "metric.json").is_file():
            load_evaluator_spec(root, legacy_ok=False)
            if not all(
                (root / name).is_file() for name in ("HANDOFF.md", "pyproject.toml")
            ):
                return None
            if not (root / "labels.csv").is_file() and not (root / "labels").is_dir():
                return None
    except (
        ArtifactNotFoundError,
        ArtifactIntegrityError,
        InvalidArtifactRefError,
        TypeError,
        ValueError,
        OSError,
    ):
        # A stale or malformed checkpoint must be rebuilt by the evaluator agent.
        return None
    return ref


def evaluator_tasks(runtime: Any, task: str) -> tuple[str, str]:
    """Render SEARCH and FINAL tasks for CSV or directory data."""
    final_labels = runtime.workspaces_root / "data_split" / "final_labels.csv"
    # GUI may reference an existing external split. Its durable contract is
    # sufficient; do not require a second copy in the local workspace.
    state = getattr(runtime, "state", None)
    if final_labels.is_file() or getattr(state, "data_contract", None):
        search = task
        if (
            getattr(state, "data_contract", None)
            and "Build the complete evaluator under evaluate/" not in task
        ):
            search = f"{task}\n\n{PLATFORM_EVALUATOR_CONTRACT}"
        configured_root = getattr(state, "data_root", None)
        if configured_root:
            final_source = Path(configured_root).resolve() / "final_labels.csv"
            final_rule = (
                f"Take the final labels from the platform-owned {final_source}; "
                "copy them only into your FINAL evaluator workspace, never into "
                "the SEARCH or training workspace."
            )
        else:
            final_rule = (
                "Take the final labels from final_labels.csv in the platform's "
                "data_split directory, and copy them into your workspace."
            )
        # ``DataContract.evaluator_task`` already carries the full generic
        # contract in the platform-split path.  Keep direct legacy callers
        # unchanged while adding the contract there in production.
        return search, final_rule
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
    name: str,
    task: str,
) -> str:
    """Bind one evaluator agent to one directory and freeze its output."""
    # The agent owns the role workspace, while ``evaluate/`` is the only
    # authoritative bundle root recorded in its descriptor.  Keeping these
    # distinct lets the prompt enforce an evaluate/ subdirectory without
    # accidentally creating evaluate/evaluate/.
    agent_workspace = runtime.workspaces_root / name
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
    split_name = (
        "final_features.csv"
        if name == FINAL_EVALUATOR_PLAN_ID
        else "search_features.csv"
    )
    split_root = runtime.workspaces_root / "data_split"
    data_root = getattr(getattr(runtime, "state", None), "data_root", None)
    expected_features = (
        Path(data_root).resolve() / split_name if data_root else split_root / split_name
    )
    # Directory datasets and legacy callers have no platform CSV split. When a
    # platform data contract exists, the matching phase feature file is required.
    has_platform_split = bool(
        getattr(getattr(runtime, "state", None), "data_contract", None)
    )
    if expected_features.is_file():
        features_arg: Path | None = expected_features
    elif has_platform_split:
        raise ValueError(
            f"platform {name} evaluator cannot freeze without its corresponding "
            f"feature split: {expected_features}"
        )
    else:
        features_arg = None
    return await run_evaluator_plan(
        runtime,
        evaluator_dir,
        task,
        name,
        MAX_PLAN_TURNS,
        expected_features=features_arg,
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
        EVALUATOR_PLAN_ID,
        task,
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
        FINAL_EVALUATOR_PLAN_ID,
        task,
    )
    return ref


def _validate_evaluator_pair(runtime: Any) -> None:
    """Validate the frozen SEARCH and FINAL id contracts and row isolation."""
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
    platform_split = (roots / "data_split" / "final_labels.csv").is_file()
    if not platform_split:
        for spec in (search_spec, final_spec):
            if spec is not None and spec.prediction_id_column != "__athena_row_id":
                raise RuntimeError(
                    "directory evaluators must use prediction id column "
                    "__athena_row_id"
                )
    if search_spec is not None and final_spec is not None:
        search_metric = (
            search_spec.task_type,
            search_spec.primary_metric,
            search_spec.class_labels,
        )
        final_metric = (
            final_spec.task_type,
            final_spec.primary_metric,
            final_spec.class_labels,
        )
        if search_metric != final_metric:
            raise RuntimeError(
                "SEARCH and FINAL evaluators declare different metric contracts"
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


async def prepare_evaluators(
    runtime: Any,
    task: str,
) -> EvaluatorBundle:
    """Freeze only the visible SEARCH evaluator during PREPARE."""
    search_task, _final_rule = evaluator_tasks(runtime, task)
    search_task += (
        "\nBuild SEARCH only. Do not read FINAL features or labels, and do not "
        "copy FINAL data into this workspace."
    )
    search_ref = await _search_evaluator(runtime, search_task)
    return EvaluatorBundle(search_ref=search_ref)


async def prepare_final_evaluator(runtime: Any, task: str) -> Any:
    """Provision FINAL after model selection, immediately before VALIDATE.

    The caller must invoke this only after a frozen SOTA exists.  Reusing a
    valid checkpoint keeps interrupted VALIDATE runs safe and avoids creating
    a second evaluator.  This function is intentionally not called by
    ``run_prepare_phase``.
    """
    search_ref = await reusable_ref(runtime, runtime.supervisor.evaluator_ref)
    if search_ref is None:
        raise RuntimeError("FINAL evaluator requires a valid frozen SEARCH evaluator")
    _, final_rule = evaluator_tasks(runtime, task)
    search_root = runtime.workspaces_root / "evaluator" / "evaluate"
    if not search_root.is_dir():
        search_root = runtime.workspaces_root / "evaluator"
    final_dir = runtime.workspaces_root / "final_evaluator" / "evaluate"
    final_task = final_evaluator_task(task, final_dir, final_rule)
    if (search_root / "metric.json").is_file():
        search_spec = load_evaluator_spec(search_root, legacy_ok=False)
        final_task += (
            "\nMatch the frozen SEARCH metric exactly: "
            f"task_type={search_spec.task_type}, "
            f"primary_metric={search_spec.primary_metric}, class_labels="
            f"{json.dumps(search_spec.class_labels, ensure_ascii=False)}."
        )
    final_ref = await _final_evaluator(runtime, final_task)
    _validate_evaluator_pair(runtime)
    await runtime.supervisor.checkpoint_final_evaluator(final_ref)
    return final_ref

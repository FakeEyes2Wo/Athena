"""Persist compact experiment records and deterministic research reports."""

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from athena.core.research_tree import ResearchTree
from athena.research.report import VALIDATION_SKIPPED_NOTICE
from athena.research.report import build_final_report as _build_tree_report

_STAGES = frozenset({"baseline", "search", "final"})
_DIRECTIONS = frozenset({"maximize", "minimize"})
_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _updated_at(value: str | datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return value.isoformat() if isinstance(value, datetime) else value


def _document(value: Mapping[str, object]) -> dict[str, object]:
    stage = str(value.get("stage", ""))
    run_id = str(value.get("run_id", ""))
    status = str(value.get("status", ""))
    metric = value.get("metric", {})
    if stage not in _STAGES:
        raise ValueError(f"stage must be one of {sorted(_STAGES)}")
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be a safe non-empty file name")
    if not status.strip():
        raise ValueError("status must be non-empty")
    if not isinstance(metric, Mapping):
        raise ValueError("metric must be an object")
    metric_name = str(metric.get("name", "primary"))
    direction = str(metric.get("direction", "maximize"))
    if not metric_name.strip():
        raise ValueError("metric_name must be non-empty")
    if direction not in _DIRECTIONS:
        raise ValueError("direction must be 'maximize' or 'minimize'")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "stage": stage,
        "status": status,
        "metric": dict(metric),
        "artifacts": dict(value.get("artifacts", {})),
        "reason": dict(value.get("reason", {})),
        "provenance": dict(value.get("provenance", {})),
        "updated_at": _updated_at(value.get("updated_at")),
    }


def _atomic_write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return path


def task_metric_name(
    root: str | Path, task_understanding: Mapping[str, object] | None
) -> str:
    """Return the confirmed or frozen evaluator metric name for one project."""
    value = task_understanding.get("primary_metric") if task_understanding else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    metric_path = Path(root) / "workspaces" / "evaluator" / "evaluate" / "metric.json"
    if metric_path.is_file():
        try:
            payload = json.loads(metric_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # An unfinished evaluator has no authoritative metric yet.
            payload = {}
        name = payload.get("primary_metric") if isinstance(payload, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "primary"


def write_stage_doc(
    root: str | Path,
    document: Mapping[str, object],
) -> Path:
    """Write one run, its stage snapshot, and ``latest.json`` atomically."""
    payload = _document(document)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    directory = Path(root) / ".athena" / "exp_docs"
    run_path = _atomic_write(directory / "runs" / f"{payload['run_id']}.json", text)
    _atomic_write(directory / f"{payload['stage']}.json", text)
    _atomic_write(directory / "latest.json", text)
    return run_path


def build_final_report(
    tree: ResearchTree,
    validation: Mapping[str, object] | None = None,
    *,
    validation_skipped: bool = False,
) -> str:
    """Build ``FINAL_REPORT.md`` with scores and deterministic cause analysis."""
    report = _build_tree_report(
        tree, validation, validation_skipped=validation_skipped
    ).rstrip()
    data = tree.to_dict()
    lines = [report, "", "## 结果归因"]
    for experiment_id, experiment in data["experiments"].items():
        if experiment["status"] == "FAILED":
            reason = " ".join(str(experiment.get("error") or "unknown").split())
            lines.append(f"- `{experiment_id}` failed: {reason}")
            continue
        evaluation = experiment.get("eval") or {}
        primary = evaluation.get("primary")
        role = (
            "selected as SOTA"
            if experiment_id == data.get("sota_id")
            else "did not replace SOTA"
        )
        lines.append(f"- `{experiment_id}` scored `{primary}` and was {role}.")
    if (
        not validation_skipped
        and validation
        and validation.get("generalization_gap") is not None
    ):
        lines.append(
            "- FINAL generalization gap was "
            f"`{validation['generalization_gap']}`; positive means FINAL was worse."
        )
    if len(lines) == 3:
        lines.append("- No experiment has settled yet.")
    return "\n".join(lines) + "\n"


def build_optimization_report(
    tree: ResearchTree,
    validation: Mapping[str, object] | None = None,
    *,
    validation_skipped: bool = False,
    metric_name: str = "primary",
    direction: str = "maximize",
) -> str:
    """Build concise optimization guidance from outcomes and validation gap."""
    if not metric_name.strip():
        raise ValueError("metric_name must be non-empty")
    if direction not in _DIRECTIONS:
        raise ValueError("direction must be 'maximize' or 'minimize'")
    if validation_skipped and validation:
        raise ValueError("validation cannot be both skipped and present")

    data = tree.to_dict()
    experiments = data["experiments"]
    successful = [
        experiment_id
        for experiment_id, experiment in experiments.items()
        if experiment["status"] == "SUCCEEDED"
    ]
    failed = [
        (experiment_id, experiment)
        for experiment_id, experiment in experiments.items()
        if experiment["status"] == "FAILED"
    ]
    lines = [
        "# Optimization",
        "",
        f"- Metric: `{metric_name}`",
        f"- Direction: `{direction}`",
        f"- Successful experiments: {len(successful)}",
        f"- Failed experiments: {len(failed)}",
    ]
    if validation_skipped:
        lines.extend(["", "## Validation", f"- {VALIDATION_SKIPPED_NOTICE}"])

    # Compare the operational SOTA with the baseline before proposing more search.
    baseline = experiments.get("exp_baseline")
    sota = experiments.get(data.get("sota_id")) if data.get("sota_id") else None
    baseline_metric = (baseline.get("eval") or {}).get("primary") if baseline else None
    sota_metric = (sota.get("eval") or {}).get("primary") if sota else None
    if isinstance(baseline_metric, (int, float)) and isinstance(
        sota_metric, (int, float)
    ):
        improvement = (
            sota_metric - baseline_metric
            if direction == "maximize"
            else baseline_metric - sota_metric
        )
        lines.extend(
            [
                "",
                "## Observed signal",
                f"- Baseline: `{baseline_metric:.6f}`",
                f"- SOTA: `{sota_metric:.6f}`",
                f"- Direction-aware improvement: `{improvement:+.6f}`",
            ]
        )
        if improvement <= 0 and len(successful) > 1:
            lines.append(
                "Search has not beaten the baseline; prioritize new evidence-backed "
                "hypotheses over extra tuning of the same model family."
            )
        elif improvement > 0:
            lines.append(
                "Freeze the winning commit, then replicate it and ablate its changed "
                "components before combining more interventions."
            )

    # A positive gap already means worse FINAL performance for either direction.
    gap = (
        validation.get("generalization_gap")
        if validation and not validation_skipped
        else None
    )
    if isinstance(gap, (int, float)) and not isinstance(gap, bool):
        lines.extend(["", f"Generalization gap: `{gap:.4f}`"])
        if gap > 0:
            lines.append(
                "The final result is worse in the configured direction; "
                "prioritize overfitting checks, simpler features, and stronger "
                "group-disjoint validation."
            )
        elif gap < 0:
            lines.append(
                "The final result is better in the configured direction; "
                "recheck split parity and preserve the validated configuration."
            )
        else:
            lines.append("No measurable generalization gap was recorded.")

    # Convert observed failures into bounded engineering actions.
    if failed:
        lines.extend(["", "## Failure-driven actions"])
        for experiment_id, experiment in failed:
            reason = str(experiment.get("error") or "unspecified failure").strip()
            lines.append(f"- `{experiment_id}`: {reason}")
        reasons = " ".join(str(item[1].get("error") or "").lower() for item in failed)
        if "modulenotfounderror" in reasons or "dependency" in reasons:
            lines.append(
                "Action: freeze dependencies and run a one-command environment "
                "preflight before spending another SEARCH attempt."
            )
        if any(token in reasons for token in ("scoring", "prediction", "evaluator")):
            lines.append(
                "Action: validate prediction ids, columns, row counts, and evaluator "
                "entrypoint before model training."
            )
        if "no_change" in reasons or "diff_rejected" in reasons:
            lines.append(
                "Action: require the next hypothesis to name the exact source file "
                "and measurable intervention before dispatch."
            )
        lines.append("Repair the observed failure class before adding more variants.")
    elif successful:
        lines.extend(
            ["", "Keep the strongest successful configuration as the reference."]
        )
    else:
        lines.extend(["", "No completed experiment is available for optimization yet."])
    return "\n".join(lines) + "\n"


def write_reports(
    root: str | Path,
    tree: ResearchTree,
    validation: Mapping[str, object] | None = None,
    *,
    validation_skipped: bool = False,
    metric_name: str = "primary",
    direction: str = "maximize",
) -> tuple[Path, Path]:
    """Write ``FINAL_REPORT.md`` and ``OPTIMIZATION.md`` atomically."""
    directory = Path(root) / ".athena" / "exp_docs"
    final_path = _atomic_write(
        directory / "FINAL_REPORT.md",
        build_final_report(tree, validation, validation_skipped=validation_skipped),
    )
    optimization_path = _atomic_write(
        directory / "OPTIMIZATION.md",
        build_optimization_report(
            tree,
            validation,
            validation_skipped=validation_skipped,
            metric_name=metric_name,
            direction=direction,
        ),
    )
    return final_path, optimization_path


if __name__ == "__main__":
    print(
        write_stage_doc(
            ".",
            {"run_id": "example", "stage": "search", "status": "SUCCEEDED"},
        )
    )

"""Pure, deterministic renderers for experiment-document projections."""

import hashlib
import json
from collections.abc import Mapping

from athena.core.research_tree import ResearchTree
from athena.research.experiment_documents.models import (
    Direction,
    LatestManifest,
    ProjectionKind,
    StageName,
    StageRecord,
)
from athena.research.report import VALIDATION_SKIPPED_NOTICE, build_final_report


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    """Serialize a mapping using the projection's canonical JSON format."""
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return f"{text}\n".encode("utf-8")


def render_stage_record(record: StageRecord) -> bytes:
    """Render one validated stage record as canonical UTF-8 JSON bytes."""
    return _json_bytes(record.model_dump(mode="json"))


def build_latest_manifest(
    *,
    kind: ProjectionKind,
    stage: StageName | None,
    run_id: str | None,
    files: Mapping[str, bytes],
) -> LatestManifest:
    """Build a content-addressed manifest from the effective output bytes."""
    digests = {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(files.items())
    }
    identity = _json_bytes(
        {
            "schema_version": 1,
            "kind": kind,
            "stage": stage,
            "run_id": run_id,
            "files": digests,
        }
    )
    return LatestManifest(
        schema_version=1,
        projection_id=hashlib.sha256(identity).hexdigest(),
        kind=kind,
        stage=stage,
        run_id=run_id,
        files=digests,
    )


def render_latest_manifest(manifest: LatestManifest) -> bytes:
    """Render a manifest using the same canonical JSON format as stage records."""
    return _json_bytes(manifest.model_dump(mode="json"))


def render_final_report(
    tree: ResearchTree,
    validation: Mapping[str, object] | None,
    *,
    validation_skipped: bool,
) -> bytes:
    """Render the shared final report without changing its text or bytes."""
    return build_final_report(
        tree, validation, validation_skipped=validation_skipped
    ).encode("utf-8")


def _markdown_cell(value: object) -> str:
    """Escape dynamic Markdown cell content without allowing row breaks."""
    text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_optimization_report(
    tree: ResearchTree,
    validation: Mapping[str, object] | None,
    *,
    metric_name: str,
    direction: Direction,
    validation_skipped: bool,
) -> bytes:
    """Render a deterministic, direction-aware optimization trajectory."""
    if not metric_name.strip():
        raise ValueError("metric_name must be non-empty")
    if direction not in {"maximize", "minimize"}:
        raise ValueError("direction must be 'maximize' or 'minimize'")
    if validation_skipped and validation:
        raise ValueError("validation cannot be both skipped and present")

    data = tree.to_dict()
    experiments = data.get("experiments") or {}
    sota_id = data.get("sota_id")
    successful = [
        experiment_id
        for experiment_id, experiment in experiments.items()
        if experiment.get("status") == "SUCCEEDED"
    ]
    failed = [
        (experiment_id, experiment)
        for experiment_id, experiment in experiments.items()
        if experiment.get("status") == "FAILED"
    ]
    scored: list[tuple[float, str, Mapping[str, object]]] = []
    unscored: list[tuple[str, Mapping[str, object]]] = []
    for experiment_id, experiment in experiments.items():
        primary = (experiment.get("eval") or {}).get("primary")
        if isinstance(primary, (int, float)) and not isinstance(primary, bool):
            scored.append((float(primary), experiment_id, experiment))
        else:
            unscored.append((experiment_id, experiment))

    if direction == "maximize":
        scored.sort(key=lambda item: (-item[0], item[1]))
    else:
        scored.sort(key=lambda item: (item[0], item[1]))

    lines = [
        "# 优化轨迹",
        "",
        f"指标：`{_markdown_cell(metric_name)}`（{direction}）",
        "",
        "| 排名 | 实验 | 指标 | 状态 | SOTA |",
        "|---|---|---|---|---|",
    ]
    for rank, (primary, experiment_id, experiment) in enumerate(scored, start=1):
        mark = "是" if experiment_id == sota_id else ""
        lines.append(
            f"| {rank} | `{_markdown_cell(experiment_id)}` | {primary:.6f} | "
            f"{_markdown_cell(experiment.get('status', ''))} | {mark} |"
        )
    if not scored:
        lines.append("| — | 尚无带分数的实验 | — | — | — |")
    if unscored:
        lines += ["", "## 未产出分数", ""]
        for experiment_id, experiment in unscored:
            reason = experiment.get("error") or experiment.get("status") or "未知"
            lines.append(
                f"- `{_markdown_cell(experiment_id)}`：{_markdown_cell(reason)}"
            )
    if validation_skipped:
        lines += ["", "## VALIDATE", "", f"- {VALIDATION_SKIPPED_NOTICE}"]
    elif validation:
        final_score = validation.get("final_test_score")
        lines += [
            "",
            "## VALIDATE",
            "",
            f"- final_test_score：{_markdown_cell(final_score)}",
            f"- search 参考：{_markdown_cell(validation.get('test_score'))}",
            f"- 泛化差：{_markdown_cell(validation.get('generalization_gap'))}",
        ]
    # Preserve the summary and guidance emitted by the legacy optimization report.
    lines[4:4] = [
        f"- Successful experiments: {len(successful)}",
        f"- Failed experiments: {len(failed)}",
    ]
    baseline = experiments.get("exp_baseline")
    sota = experiments.get(sota_id) if sota_id else None
    baseline_metric = (baseline.get("eval") or {}).get("primary") if baseline else None
    sota_metric = (sota.get("eval") or {}).get("primary") if sota else None
    if (
        isinstance(baseline_metric, (int, float))
        and not isinstance(baseline_metric, bool)
        and isinstance(sota_metric, (int, float))
        and not isinstance(sota_metric, bool)
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

    gap = (
        validation.get("generalization_gap")
        if validation and not validation_skipped
        else None
    )
    if isinstance(gap, (int, float)) and not isinstance(gap, bool):
        lines.extend(["", f"Generalization gap: `{gap:.4f}`"])
        if gap > 0:
            lines.append(
                "The final result is worse in the configured direction; prioritize "
                "overfitting checks, simpler features, and stronger group-disjoint "
                "validation."
            )
        elif gap < 0:
            lines.append(
                "The final result is better in the configured direction; recheck "
                "split parity and preserve the validated configuration."
            )
        else:
            lines.append("No measurable generalization gap was recorded.")

    if failed:
        lines.extend(["", "## Failure-driven actions"])
        for experiment_id, experiment in failed:
            reason = str(experiment.get("error") or "unspecified failure").strip()
            lines.append(
                f"- `{_markdown_cell(experiment_id)}`: {_markdown_cell(reason)}"
            )
        reasons = " ".join(
            str(experiment.get("error") or "").lower() for _, experiment in failed
        )
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
    return ("\n".join(lines) + "\n").encode("utf-8")


__all__ = [
    "build_latest_manifest",
    "render_final_report",
    "render_latest_manifest",
    "render_optimization_report",
    "render_stage_record",
]

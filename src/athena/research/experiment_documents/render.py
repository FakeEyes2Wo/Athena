"""Pure, deterministic renderers for experiment-document projections."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

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


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:.6f}"


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
    return ("\n".join(lines) + "\n").encode("utf-8")


__all__ = [
    "build_latest_manifest",
    "render_final_report",
    "render_latest_manifest",
    "render_optimization_report",
    "render_stage_record",
]

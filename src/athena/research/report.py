"""Deterministic final research report builder.

The report is a pure function of the ``ResearchTree`` and an optional VALIDATE
result dict, so both the Supervisor (after VALIDATE completes) and the GUI
(``generate_report``) render the same artifact without duplicating logic.
"""

from collections.abc import Mapping
from typing import Any, Literal

from athena.core.research_tree import ResearchTree

VALIDATION_SKIPPED_NOTICE = (
    "VALIDATE 已跳过。本报告仅使用 SEARCH 结果，不包含独立最终测试分数或泛化差距。"
)


def _number(value: Any, *, signed: bool = False) -> str:
    """Format a metric for the report; non-numbers pass through as text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:+.4f}" if signed else f"{value:.4f}"


def _score(value: object) -> int | float | None:
    """Return a numeric score while rejecting bools masquerading as integers."""
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _primary(value: object) -> str:
    """Preserve the compact integer and four-decimal float report format."""
    return f"{value:.4f}" if isinstance(value, float) else str(value)


def _sota_section(data: Mapping[str, Any]) -> list[str]:
    """Render the SOTA block, if the tree has one."""
    sota_id = data.get("sota_id")
    if not sota_id:
        return []
    sota_experiment = data["experiments"][sota_id]
    primary = (
        sota_experiment["eval"]["primary"] if sota_experiment.get("eval") else None
    )
    hypothesis = data["hypotheses"][sota_experiment["hypothesis_id"]]
    lines = ["", "## SOTA"]
    lines.append(f"- **SOTA 实验**: `{sota_id}`")
    if primary is not None:
        lines.append(f"- **最佳 primary**: {_primary(primary)}")
    lines.append(f"- **SOTA 假设**: {hypothesis['statement']}")
    secondary = (
        sota_experiment["eval"].get("secondary") or {}
        if sota_experiment.get("eval")
        else {}
    )
    if secondary:
        rendered = ", ".join(
            f"{name} {_number(value)}" for name, value in sorted(secondary.items())
        )
        lines.append(f"- **次要指标**: {rendered}")
    lines.append("")
    return lines


def _validation_section(
    validation: Mapping[str, Any] | None, *, validation_skipped: bool = False
) -> list[str]:
    """Render optional VALIDATE results even when the tree has no SOTA yet."""
    if validation_skipped:
        return ["", "## 验证结果", f"- {VALIDATION_SKIPPED_NOTICE}"]
    if not validation:
        return []
    final_score = validation.get("final_test_score")
    gap = validation.get("generalization_gap")
    warning = validation.get("generalization_warning")
    if final_score is None and gap is None and not warning:
        return []
    lines = ["", "## 验证结果"]
    if final_score is not None:
        lines.append(f"- **最终测试分数**: {_primary(final_score)}")
    if gap is not None:
        lines.append(f"- **泛化差距**: {_primary(gap)}")
    if warning:
        lines.append("- **泛化警告**: 测试集表现与训练/验证集差距过大，存在过拟合风险")
    return lines


def _iteration_table_section(data: Mapping[str, Any], sota_id: str | None) -> list[str]:
    """Render the per-experiment primary-vs-SOTA comparison table."""
    scored = [
        (exp_id, experiment)
        for exp_id, experiment in data["experiments"].items()
        if experiment.get("eval") and experiment["eval"].get("primary") is not None
    ]
    if not scored:
        return []
    reference = (
        data["experiments"][sota_id]["eval"]["primary"]
        if sota_id and data["experiments"][sota_id].get("eval")
        else None
    )
    lines = ["", "## 迭代对照", ""]
    lines.append("| 实验 | 状态 | primary | Δ vs SOTA | 判定 | 假设 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for exp_id, experiment in scored:
        primary = experiment["eval"]["primary"]
        delta = (
            _number(primary - reference, signed=True)
            if isinstance(primary, (int, float)) and isinstance(reference, (int, float))
            else "—"
        )
        verdict = experiment.get("verdict") or {}
        winner = verdict.get("winner")
        p_value = verdict.get("p_value")
        judged = (
            f"{winner} (p={_number(p_value)})"
            if winner and p_value is not None
            else (winner or "—")
        )
        statement = data["hypotheses"][experiment["hypothesis_id"]]["statement"]
        lines.append(
            f"| `{exp_id}` | {experiment['status']} | {_number(primary)} | "
            f"{delta} | {judged} | {statement} |"
        )
    return lines


def _failed_experiments_section(data: Mapping[str, Any]) -> list[str]:
    """Render failed experiments so 'ran 8 only succeeded 2' is visible."""
    failed = [
        (exp_id, experiment)
        for exp_id, experiment in data["experiments"].items()
        if experiment.get("error")
    ]
    if not failed:
        return []
    lines = ["", "## 失败实验与原因"]
    for exp_id, experiment in failed:
        reason = " ".join(str(experiment["error"]).split())[:300]
        lines.append(f"- **{exp_id}** [{experiment['status']}] {reason}")
    return lines


def _next_steps_section(
    tree: ResearchTree, data: Mapping[str, Any], executed: set[str]
) -> list[str]:
    """Render pending hypotheses and the intervention/observation plan."""
    pending = [
        hypothesis
        for hypothesis in tree.pending_hypotheses()
        if hypothesis.id not in executed
    ]
    if not pending:
        return []
    lines = ["", "## 待选假设"]
    for hypothesis in pending:
        lines.append(f"- {hypothesis.statement}")
    lines.append("")
    lines.append("## 下一步验证方案")
    for index, hypothesis in enumerate(pending, start=1):
        lines.append(f"{index}. {hypothesis.statement}")
        lines.append(f"   - 干预: {hypothesis.intervention}")
        lines.append(f"   - 预期观测: {hypothesis.expected_effect}")
    return lines


def _experiment_records_section(data: Mapping[str, Any]) -> list[str]:
    """Render every experiment in chronological/display order."""
    lines = ["", "## 实验记录"]
    for exp_id, experiment in data["experiments"].items():
        hypothesis = data["hypotheses"][experiment["hypothesis_id"]]
        primary = experiment["eval"]["primary"] if experiment.get("eval") else None
        suffix = f" — primary {_primary(primary)}" if primary is not None else ""
        lines.append(
            f"- **{exp_id}** [{experiment['status']}] "
            f"{hypothesis['statement']}{suffix}"
        )
    return lines


def build_final_report(
    tree: ResearchTree,
    validation: Mapping[str, Any] | None = None,
    *,
    validation_skipped: bool = False,
) -> str:
    """Render the research tree plus optional VALIDATE results as Markdown."""
    if validation_skipped and validation:
        raise ValueError("validation cannot be both skipped and present")
    data = tree.to_dict()
    executed = {
        experiment["hypothesis_id"] for experiment in data["experiments"].values()
    }
    lines = ["# Athena 研究报告", ""]
    lines += _sota_section(data)
    lines.append(f"- 实验总数: {len(data['experiments'])}")
    lines.append(f"- 假设总数: {len(data['hypotheses'])}")
    lines += _validation_section(validation, validation_skipped=validation_skipped)
    lines += _iteration_table_section(data, data.get("sota_id"))
    lines += _failed_experiments_section(data)
    lines += _next_steps_section(tree, data, executed)
    lines += _experiment_records_section(data)
    return "\n".join(lines)


def _markdown_cell(value: object) -> str:
    """Escape dynamic Markdown cell content without allowing row breaks."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def build_optimization_report(
    tree: ResearchTree,
    validation: Mapping[str, object] | None,
    *,
    metric_name: str,
    direction: Literal["maximize", "minimize"],
    validation_skipped: bool,
) -> str:
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
    successful = 0
    failed: list[tuple[str, Mapping[str, object]]] = []
    scored: list[tuple[float, str, Mapping[str, object]]] = []
    unscored: list[tuple[str, Mapping[str, object]]] = []
    for experiment_id, experiment in experiments.items():
        status = experiment.get("status")
        if status == "SUCCEEDED":
            successful += 1
        elif status == "FAILED":
            failed.append((experiment_id, experiment))
        score = _score((experiment.get("eval") or {}).get("primary"))
        if score is not None:
            scored.append((float(score), experiment_id, experiment))
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
    lines[4:4] = [
        f"- Successful experiments: {successful}",
        f"- Failed experiments: {len(failed)}",
    ]
    baseline = experiments.get("exp_baseline")
    sota = experiments.get(sota_id) if sota_id else None
    baseline_metric = _score(
        (baseline.get("eval") or {}).get("primary") if baseline else None
    )
    sota_metric = _score((sota.get("eval") or {}).get("primary") if sota else None)
    if baseline_metric is not None and sota_metric is not None:
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
        if improvement <= 0 and successful > 1:
            lines.append(
                "Search has not beaten the baseline; prioritize new evidence-backed "
                "hypotheses over extra tuning of the same model family."
            )
        elif improvement > 0:
            lines.append(
                "Freeze the winning commit, then replicate it and ablate its changed "
                "components before combining more interventions."
            )

    gap = _score(
        validation.get("generalization_gap")
        if validation and not validation_skipped
        else None
    )
    if gap is not None:
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
    return "\n".join(lines) + "\n"


__all__ = [
    "VALIDATION_SKIPPED_NOTICE",
    "build_final_report",
    "build_optimization_report",
]

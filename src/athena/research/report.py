"""Deterministic final research report builder.

The report is a pure function of the ``ResearchTree`` and an optional VALIDATE
result dict, so both the Supervisor (after VALIDATE completes) and the GUI
(``generate_report``) render the same artifact without duplicating logic.
"""

from collections.abc import Mapping
from typing import Any

from athena.core.research_tree import ResearchTree

VALIDATION_SKIPPED_NOTICE = (
    "VALIDATE 已跳过。本报告仅使用 SEARCH 结果，不包含独立最终测试分数或泛化差距。"
)


def _number(value: Any, *, signed: bool = False) -> str:
    """Format a metric for the report; non-numbers pass through as text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:+.4f}" if signed else f"{value:.4f}"


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
        lines.append(
            f"- **最佳 primary**: "
            f"{(f'{primary:.4f}' if isinstance(primary, float) else str(primary))}"
        )
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
        lines.append(
            f"- **最终测试分数**: "
            f"{(f'{final_score:.4f}' if isinstance(final_score, float) else str(final_score))}"
        )
    if gap is not None:
        lines.append(
            f"- **泛化差距**: "
            f"{(f'{gap:.4f}' if isinstance(gap, float) else str(gap))}"
        )
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
        suffix = (
            f" — primary "
            f"{(f'{primary:.4f}' if isinstance(primary, float) else str(primary))}"
            if primary is not None
            else ""
        )
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


__all__ = ["VALIDATION_SKIPPED_NOTICE", "build_final_report"]

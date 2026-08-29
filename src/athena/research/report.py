"""Deterministic final research report builder.

The report is a pure function of the ``ResearchTree`` and an optional VALIDATE
result dict, so both the Supervisor (after VALIDATE completes) and the GUI
(``generate_report``) render the same artifact without duplicating logic.
"""

from collections.abc import Mapping
from typing import Any

from athena.core.research_tree import ResearchTree


def _number(value: Any, *, signed: bool = False) -> str:
    """Format a metric for the report; non-numbers pass through as text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:+.4f}" if signed else f"{value:.4f}"


def build_final_report(
    tree: ResearchTree, validation: Mapping[str, Any] | None = None
) -> str:
    """Render the research tree plus optional VALIDATE results as Markdown."""
    data = tree.to_dict()
    executed = {
        experiment["hypothesis_id"] for experiment in data["experiments"].values()
    }
    lines: list[str] = ["# Athena 研究报告", ""]

    sota_id = data.get("sota_id")
    if sota_id:
        sota_experiment = data["experiments"][sota_id]
        primary = (
            sota_experiment["eval"]["primary"] if sota_experiment.get("eval") else None
        )
        hypothesis = data["hypotheses"][sota_experiment["hypothesis_id"]]
        lines.append("## SOTA")
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

    lines.append(f"- 实验总数: {len(data['experiments'])}")
    lines.append(f"- 假设总数: {len(data['hypotheses'])}")

    if validation:
        final_score = validation.get("final_test_score")
        gap = validation.get("generalization_gap")
        warning = validation.get("generalization_warning")
        if final_score is not None or gap is not None or warning:
            lines.append("")
            lines.append("## 验证结果")
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
                lines.append(
                    "- **泛化警告**: 测试集表现与训练/验证集差距过大，存在过拟合风险"
                )

    # 迭代对照表：每次改动相对 SOTA 的增减，以及判定与显著性。评审要的"关键改动
    # 前后指标对比"就是这张表；只报一个最终分数看不出任何一步是否真的有用。
    scored = [
        (exp_id, experiment)
        for exp_id, experiment in data["experiments"].items()
        if experiment.get("eval") and experiment["eval"].get("primary") is not None
    ]
    if scored:
        reference = (
            data["experiments"][sota_id]["eval"]["primary"]
            if sota_id and data["experiments"][sota_id].get("eval")
            else None
        )
        lines.append("")
        lines.append("## 迭代对照")
        lines.append("")
        lines.append("| 实验 | 状态 | primary | Δ vs SOTA | 判定 | 假设 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for exp_id, experiment in scored:
            primary = experiment["eval"]["primary"]
            delta = (
                _number(primary - reference, signed=True)
                if isinstance(primary, (int, float))
                and isinstance(reference, (int, float))
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

    # 失败实验：报告此前完全不提它们，于是"跑了 8 次只成 2 次"和"跑了 2 次都成"
    # 在最终材料里长得一模一样。
    failed = [
        (exp_id, experiment)
        for exp_id, experiment in data["experiments"].items()
        if experiment.get("error")
    ]
    if failed:
        lines.append("")
        lines.append("## 失败实验与原因")
        for exp_id, experiment in failed:
            reason = " ".join(str(experiment["error"]).split())[:300]
            lines.append(f"- **{exp_id}** [{experiment['status']}] {reason}")

    # 只列出"尚未执行"的假设（已创建实验的假设不算待选，例如 baseline）。
    pending = [
        hypothesis
        for hypothesis in tree.pending_hypotheses()
        if hypothesis.id not in executed
    ]
    if pending:
        lines.append("")
        lines.append("## 待选假设")
        for hypothesis in pending:
            lines.append(f"- {hypothesis.statement}")
        # 同一批假设再按"干预 → 预期观测"展开一次：这正是评审要的下一步验证方案，
        # 光有 statement 看不出该做什么实验、看到什么才算成立。
        lines.append("")
        lines.append("## 下一步验证方案")
        for index, hypothesis in enumerate(pending, start=1):
            lines.append(f"{index}. {hypothesis.statement}")
            lines.append(f"   - 干预: {hypothesis.intervention}")
            lines.append(f"   - 预期观测: {hypothesis.expected_effect}")

    lines.append("")
    lines.append("## 实验记录")
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
    return "\n".join(lines)


__all__ = ["build_final_report"]

"""Deterministic final research report builder.

The report is a pure function of the ``ResearchTree`` and an optional VALIDATE
result dict, so both the Supervisor (after VALIDATE completes) and the GUI
(``generate_report``) render the same artifact without duplicating logic.
"""

from collections.abc import Mapping
from typing import Any

from athena.core.research_tree import ResearchTree


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

from athena.core.schemas import Hypothesis, EvalResult, ArtifactRef
from athena.core.research.research_tree import ResearchTree


class Reporter:
    async def generate(
        self,
        sota_node_id: str,
        tree: ResearchTree,
        ablation: list[tuple[Hypothesis, EvalResult]],
        final: EvalResult,
    ) -> ArtifactRef:
        """Generate final Markdown report with full evidence chain."""
        sota = tree.get_node_by_id(sota_node_id)
        path = tree.hypotheses_path(sota_node_id)

        lines = [
            "# Athena AI4ML Experiment Report",
            "",
            "## SOTA Hypothesis Chain",
            "",
        ]
        for i, h in enumerate(path, 1):
            lines.append(
                f"{i}. **{h.statement}** — {h.intervention} → {h.expected_effect}"
            )

        lines += [
            "",
            "## Final Test Result",
            f"- Primary metric: **{final.primary:.4f}**",
            "",
            "## Ablation Results",
            "",
        ]
        for h, r in ablation:
            lines.append(
                f"- Remove **{h.statement}**: primary = {r.primary:.4f} (Δ = {final.primary - r.primary:+.4f})"
            )
        lines += [
            "",
            "## SOTA Experiment",
            f"- Metric: {sota.exp.metric_type} = {sota.exp.result}",
        ]

        report = "\n".join(lines)
        import os, tempfile

        path_out = os.path.join(
            tempfile.gettempdir(), f"athena_report_{sota_node_id[:8]}.md"
        )
        with open(path_out, "w") as f:
            f.write(report)
        return f"artifact://{path_out}"

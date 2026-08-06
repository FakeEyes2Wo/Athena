"""Generate a deterministic final report from complete ResearchTree v2 evidence."""

import json
from pathlib import Path
from tempfile import gettempdir

from pydantic import BaseModel, Field

from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.contracts import ArtifactRef
from athena.core.research_models import Hypothesis
from athena.evaluation.types import EvalResult


def _eval(exp: Experiment) -> EvalResult:
    """成功实验必有 eval（Experiment 校验器保证）。"""
    assert exp.eval is not None
    return exp.eval


class ReportNarrative(BaseModel):
    """Narrative fields that may be supplied by a structured agent."""

    executive_summary: str = Field(
        description="A concise summary grounded only in the supplied evidence."
    )
    limitations: list[str] = Field(
        description="Material limitations or uncertainty supported by the evidence."
    )
    recommendations: list[str] = Field(
        description="Concrete next actions justified by the evidence."
    )


class Reporter:
    """Render and attach a report only when validation evidence is complete."""

    def __init__(self, agent=None, output_dir: str | Path | None = None) -> None:
        self._agent = agent
        self._output_dir = Path(output_dir or gettempdir())

    async def _narrative(self, evidence: dict[str, object]) -> ReportNarrative:
        if self._agent is None:
            return ReportNarrative(
                executive_summary=(
                    "The selected experiment is reported with its complete hypothesis "
                    "path, frozen final-test metric, and ablation evidence."
                ),
                limitations=["The report reflects the recorded validation runs only."],
                recommendations=["Review the evidence chain before deployment."],
            )
        prompt = (
            "Write a concise scientific report narrative using only this evidence. "
            "Do not invent metrics, experiments, or causal claims.\n"
            f"{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}"
        )
        result = await self._agent.run(prompt, output_type=ReportNarrative)
        output = getattr(result, "output", result)
        return (
            output
            if isinstance(output, ReportNarrative)
            else ReportNarrative.model_validate(output)
        )

    @staticmethod
    def _require_evidence(experiment_id: str, experiment: Experiment) -> None:
        if experiment.status is not ExperimentStatus.SUCCEEDED:
            raise RuntimeError(
                f"complete validation requires successful {experiment_id}"
            )
        if experiment.eval is None:
            raise RuntimeError(f"complete validation requires eval for {experiment_id}")
        missing = {"diff", "logs"} - experiment.artifacts.keys()
        if missing:
            raise RuntimeError(
                f"complete validation requires {sorted(missing)} for {experiment_id}"
            )

    @staticmethod
    def _one_successful(
        tree: ResearchTree,
        experiment_ids: list[str],
        *,
        label: str,
    ) -> tuple[str, Experiment]:
        successful = [
            (experiment_id, tree.get_experiment(experiment_id))
            for experiment_id in experiment_ids
            if tree.get_experiment(experiment_id).status is ExperimentStatus.SUCCEEDED
        ]
        if len(successful) != 1:
            raise RuntimeError(
                f"complete validation requires exactly one successful {label}"
            )
        Reporter._require_evidence(*successful[0])
        return successful[0]

    async def generate(self, sota_id: str, tree: ResearchTree) -> ArtifactRef:
        """Create and attach one report from canonical tree evidence."""
        sota = tree.get_experiment(sota_id)
        if tree.best_experiment_id() != sota_id:
            raise RuntimeError("REPORT requires the selected SOTA experiment")
        if sota.status is not ExperimentStatus.SUCCEEDED or sota.eval is None:
            raise RuntimeError("REPORT requires a successful SOTA with evaluation")

        hypotheses = tree.hypotheses_path(sota_id)
        hypothesis_ids = [hypothesis.id for hypothesis in hypotheses]
        children = [
            (child_id, tree.get_experiment(child_id))
            for child_id in tree.list_children(sota_id)
        ]
        ablation_children = [
            child_id
            for child_id, experiment in children
            if experiment.plan.kind == "ablation"
        ]
        final_children = [
            child_id
            for child_id, experiment in children
            if experiment.plan.kind == "final-test"
        ]

        successful_ablation_hypotheses = {
            tree.get_experiment(child_id).hypothesis_id
            for child_id in ablation_children
            if tree.get_experiment(child_id).status is ExperimentStatus.SUCCEEDED
        }
        if successful_ablation_hypotheses != set(hypothesis_ids):
            raise RuntimeError("complete validation requires exact ablation coverage")

        ablations: list[tuple[Hypothesis, str, Experiment]] = []
        for hypothesis in hypotheses:
            matching = [
                child_id
                for child_id in ablation_children
                if tree.get_experiment(child_id).hypothesis_id == hypothesis.id
            ]
            experiment_id, experiment = self._one_successful(
                tree,
                matching,
                label=f"ablation for {hypothesis.id}",
            )
            ablations.append((hypothesis, experiment_id, experiment))

        final_test_id, final_test = self._one_successful(
            tree,
            final_children,
            label="final-test",
        )
        if final_test.hypothesis_id != sota.hypothesis_id:
            raise RuntimeError(
                "complete validation final-test targets the wrong hypothesis"
            )

        evidence = {
            "sota_id": sota_id,
            "sota_primary": _eval(sota).primary,
            "final_test": {
                "experiment_id": final_test_id,
                "primary": _eval(final_test).primary,
            },
            "hypotheses": [
                hypothesis.model_dump(mode="json") for hypothesis in hypotheses
            ],
            "ablations": [
                {
                    "experiment_id": experiment_id,
                    "hypothesis_id": hypothesis.id,
                    "primary": _eval(experiment).primary,
                    "delta_from_final": _eval(final_test).primary
                    - _eval(experiment).primary,
                }
                for hypothesis, experiment_id, experiment in ablations
            ],
        }
        narrative = await self._narrative(evidence)

        lines = [
            "# Athena AI4ML Experiment Report",
            "",
            "## Executive Summary",
            "",
            narrative.executive_summary,
            "",
            "## SOTA Hypothesis Path",
            "",
        ]
        for index, hypothesis in enumerate(hypotheses, 1):
            lines.append(
                f"{index}. **{hypothesis.statement}**: "
                f"{hypothesis.intervention} -> {hypothesis.expected_effect}"
            )
        lines.extend(
            [
                "",
                "## Final Test",
                "",
                f"Final primary: {_eval(final_test).primary:.4f}",
                f"Final experiment: `{final_test_id}`",
                "",
                "## Ablation Evidence",
                "",
            ]
        )
        for hypothesis, experiment_id, experiment in ablations:
            delta = _eval(final_test).primary - _eval(experiment).primary
            lines.append(
                f"- Ablation `{experiment_id}` for `{hypothesis.id}`: "
                f"primary {_eval(experiment).primary:.4f}, delta {delta:+.4f}"
            )
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {item}" for item in narrative.limitations)
        lines.extend(["", "## Recommendations", ""])
        lines.extend(f"- {item}" for item in narrative.recommendations)

        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / f"athena_report_{sota_id[:8]}.md"
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report_ref = f"artifact://{output_path}"
        tree.attach_artifact(sota_id, "report", report_ref)
        return report_ref

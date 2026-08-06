"""Create and execute the single PREPARE baseline experiment."""

import asyncio

from pydantic import BaseModel, Field

from athena.core.workspace import GitWorkspace
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.contracts import new_id
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.evaluation.types import EvalSpec
from athena.data.types import DataProfile, ProcessingLog
from athena.workflows.search.code_agent import CodeAgent, CodeExecutionError


class BaselineDraft(BaseModel):
    """Structured proposal used to configure the baseline experiment."""

    statement: str = Field(description="A concise claim defining the baseline.")
    intervention: str = Field(
        description="The exact training procedure applied to the cleaned training split."
    )
    expected_effect: str = Field(
        description="The measurable role of the baseline in later comparisons."
    )
    change: str = Field(description="The code and configuration change to execute.")
    budget: dict[str, int | float | str] = Field(
        description="A small, explicit resource budget for baseline execution."
    )
    acceptance_rule: str = Field(
        description="The deterministic rule for accepting a completed baseline."
    )
    rubrics: list[str] = Field(
        min_length=1,
        description="Evidence-based criteria the baseline implementation must satisfy.",
    )


def _default_draft() -> BaselineDraft:
    return BaselineDraft(
        statement="A reproducible default model establishes the comparison baseline",
        intervention="Train one default model on the cleaned training split",
        expected_effect="Produce the reference primary metric for later experiments",
        change="Implement and execute the simplest task-appropriate baseline",
        budget={"max_trials": 1},
        acceptance_rule="The run completes and emits the frozen evaluation protocol",
        rubrics=[
            "Uses only the cleaned training split",
            "Does not access the final test split",
            "Records code, configuration, logs, and metric artifacts",
        ],
    )


async def _draft_with_agent(
    agent,
    data_profile: DataProfile,
    processing_log: ProcessingLog,
) -> BaselineDraft:
    prompt = (
        "Design one reproducible baseline experiment for the prepared dataset.\n"
        f"Data profile: {data_profile.model_dump_json()}\n"
        f"Prepared artifacts: {processing_log.model_dump_json()}\n"
        "Use only the cleaned training split. Do not use the final test split."
    )
    result = await agent.run(prompt, output_type=BaselineDraft)
    output = getattr(result, "output", result)
    return (
        output
        if isinstance(output, BaselineDraft)
        else BaselineDraft.model_validate(output)
    )


async def create_baseline(
    workspace: GitWorkspace,
    base_commit: str,
    research_tree: ResearchTree,
    *,
    data_profile: DataProfile,
    processing_log: ProcessingLog,
    eval_spec: EvalSpec,
    code_agent: CodeAgent,
    agent=None,
) -> str:
    """Execute the sole baseline and return its canonical experiment ID."""
    roots = research_tree.root_experiment_ids()
    baselines = [
        experiment_id
        for experiment_id in roots
        if research_tree.get_experiment(experiment_id).plan.kind == "baseline"
    ]
    if baselines:
        baseline_id = baselines[0]
        baseline = research_tree.get_experiment(baseline_id)
        if baseline.status is ExperimentStatus.SUCCEEDED:
            return baseline_id
        raise RuntimeError(f"{baseline.status.value.lower()} baseline blocks SEARCH")
    if roots:
        raise RuntimeError("cannot create a baseline after another root experiment")

    required_splits = {"train", "validation", "test"}
    if required_splits - processing_log.splits.keys():
        raise ValueError(
            "baseline requires train, validation, and test split artifacts"
        )

    draft = (
        await _draft_with_agent(agent, data_profile, processing_log)
        if agent is not None
        else _default_draft()
    )
    hypothesis_id = new_id("hyp")
    experiment_id = new_id("exp")
    worktree = await workspace.create(base_commit, f"baseline/{experiment_id}")
    hypothesis = Hypothesis(
        id=hypothesis_id,
        statement=draft.statement,
        intervention=draft.intervention,
        expected_effect=draft.expected_effect,
        evidence_refs=[
            processing_log.cleaned_data,
            processing_log.splits["train"],
        ],
        status="PROPOSED",
    )
    plan = ExperimentPlan(
        kind="baseline",
        change=draft.change,
        run_config_ref=f"artifact://runs/{experiment_id}/config",
        budget=draft.budget,
        acceptance_rule=draft.acceptance_rule,
        rubrics=draft.rubrics,
    )
    research_tree.add_hypothesis(hypothesis)
    research_tree.add_experiment(
        experiment_id,
        Experiment(
            hypothesis_id=hypothesis_id,
            commit=base_commit,
            plan=plan,
            gitwork=worktree,
        ),
    )
    research_tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)

    try:
        result = await code_agent.execute(
            experiment_id,
            hypothesis,
            plan,
            base_commit,
            eval_spec,
            worktree,
        )
    except asyncio.CancelledError:
        research_tree.transition_experiment(experiment_id, ExperimentStatus.CANCELLED)
        raise
    except CodeExecutionError as exc:
        research_tree.attach_artifact(experiment_id, "logs", exc.logs)
        research_tree.transition_experiment(
            experiment_id,
            ExperimentStatus.FAILED,
            error=str(exc),
        )
        raise
    except Exception as exc:
        research_tree.transition_experiment(
            experiment_id,
            ExperimentStatus.FAILED,
            error=str(exc) or type(exc).__name__,
        )
        raise

    research_tree.complete_experiment(
        experiment_id,
        eval=result.eval,
        verdict=None,
        artifacts={"diff": result.diff, "logs": result.logs},
    )
    research_tree.update_hypothesis_status(hypothesis_id, "SUPPORTED")
    research_tree.set_sota(experiment_id)
    return experiment_id

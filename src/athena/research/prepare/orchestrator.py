"""Orchestrate the functional stages of PREPARE."""

from typing import Any

from athena.research.clarification.context import (
    confirmed_task_context_block,
    task_prompt,
)
from athena.research.prepare.baseline import (
    HandoffFn,
    directory_candidate_task,
    prepare_baseline_design,
    run_baseline,
)
from athena.research.prepare.data import prepare_platform_split
from athena.research.prepare.eda import prepare_eda, prepare_workspace
from athena.research.prepare.evaluator import prepare_evaluators
from athena.research.supervisor.prepare import PrepareResult


async def run_prepare_phase(
    runtime: Any, run_handoff_agent: HandoffFn
) -> PrepareResult:
    """Orchestrate data, evaluators, EDA, and the trusted baseline."""
    task_context = await confirmed_task_context_block(runtime)
    if runtime.prepare_phase is not None:
        return await runtime.prepare_phase()
    if runtime.provider is None:
        raise RuntimeError("PREPARE requires a registered Agent provider")

    # Establish the shared task and durable data contract.
    base_task = task_prompt(runtime.task_text, task_context)
    workspace = await prepare_workspace(runtime)
    contract = await prepare_platform_split(runtime)
    evaluator_task = contract.evaluator_task(base_task) if contract else base_task
    candidate_task = (
        contract.candidate_task(base_task)
        if contract
        else directory_candidate_task(base_task)
    )

    # Freeze evaluation before generating and scoring the baseline.
    evaluators = await prepare_evaluators(runtime, evaluator_task)
    eda_ready = await prepare_eda(runtime, workspace, run_handoff_agent, base_task)
    verified = await prepare_baseline_design(
        runtime, workspace, candidate_task, eda_ready, run_handoff_agent
    )
    predict_features = contract.predict_features_csv if contract else None
    return await run_baseline(
        runtime,
        workspace,
        evaluators.search_ref,
        candidate_task,
        predict_features,
        verified,
    )

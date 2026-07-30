"""Baseline builder tools — solution design, code generation, execution, submission.

solution_design: Design solution approach based on research, with rubric self-check.
project_code_gen: Generate complete project code from a solution plan.
code_execute: Execute code in a sandbox, capture logs and results.
submission_build: Package predictions into competition submission format.
"""

import json

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat

# ── System prompts ──

_SOLUTION_SYSTEM_PROMPT = """\
You are an ML competition strategist. Given task metadata, EDA report,
research summaries, and available models, design a solution approach.

Output a JSON with:
- model_selection: which model(s) to use and why
- pipeline_structure: data -> preprocessing -> model -> postprocessing
- training_strategy: validation split, CV folds, early stopping
- loss_metric: which loss function and why
- rubric: a self-check list of criteria the solution MUST satisfy

The rubric must include at minimum: correctness, feasibility, and
submission format compliance.
"""

_CODE_GEN_SYSTEM_PROMPT = """\
You are an ML engineer. Given a solution plan, generate a complete,
runnable Python project with these files:

1. model.py — Model definition using PyTorch or sklearn
2. dataset.py — Dataset/dataloader using cleaned data
3. train.py — Training loop with validation, checkpoint saving
4. infer.py — Inference on test set, output predictions
5. config.yaml — All hyperparameters and paths

Output each file with its path and content clearly labeled.
"""


# ── Solution Design Tool ──


class SolutionDesignTool(BaseTool):
    """Design a competition solution approach based on research and EDA."""

    spec = ToolSpec(
        name="solution_design",
        description=(
            "Design a competition solution approach based on task metadata, "
            "EDA report, research findings, and available models. Returns a "
            "solution plan with a rubric self-check list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_metadata": {
                    "type": "object",
                    "description": "TaskMetaData as a dict (task_type, data_type, etc.).",
                },
                "eda_report_ref": {
                    "type": "string",
                    "description": "Artifact reference to the EDA report JSON.",
                },
                "research_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Artifact references to research/discussion summaries.",
                },
                "model_candidates": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of available model candidates from hf_model_search.",
                },
            },
            "required": ["task_metadata", "eda_report_ref", "research_refs", "model_candidates"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        user_prompt = json.dumps(input, ensure_ascii=False)

        result_text = await single_turn_chat(
            system_prompt=_SOLUTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        plan = json.loads(result_text)

        # Validate rubric must exist
        if "rubric" not in plan or not plan["rubric"]:
            return ToolResult(
                success=False,
                data=None,
                error="Solution plan is missing required 'rubric' field. "
                      "The plan must include a self-check rubric.",
            )

        return ToolResult(data={"solution_plan": plan})


# ── Project Code Gen Tool ──


class ProjectCodeGenTool(BaseTool):
    """Generate complete project code from a solution plan."""

    spec = ToolSpec(
        name="project_code_gen",
        description=(
            "Generate a complete, runnable Python project from a solution plan. "
            "Produces model.py, dataset.py, train.py, infer.py, and config.yaml."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "solution_plan_ref": {
                    "type": "string",
                    "description": "Artifact reference to the solution plan JSON.",
                },
                "data_card_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "DataCard references for the datasets.",
                },
                "model_path": {
                    "type": "string",
                    "description": "Local path to downloaded model weights (if any).",
                },
                "submission_format": {
                    "type": "string",
                    "description": "Description of the required submission format.",
                },
            },
            "required": ["solution_plan_ref", "data_card_refs", "submission_format"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        user_prompt = json.dumps(input, ensure_ascii=False)

        code_text = await single_turn_chat(
            system_prompt=_CODE_GEN_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        return ToolResult(
            data={
                "code": code_text,
                "solution_plan_ref": input["solution_plan_ref"],
                "status": "code_generated",
            }
        )


# ── Code Execute Tool ──


class CodeExecuteTool(BaseTool):
    """Execute code in sandbox (train or infer)."""

    spec = ToolSpec(
        name="code_execute",
        description=(
            "Execute Python code in an isolated sandbox. Use entry_point 'train' "
            "to run training (produces checkpoint) or 'infer' to run inference "
            "(produces predictions). Returns logs, metrics, and output paths."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code_artifact_ref": {
                    "type": "string",
                    "description": "Artifact reference to the generated code.",
                },
                "entry_point": {
                    "type": "string",
                    "enum": ["train", "infer"],
                    "description": "Which script to execute.",
                },
            },
            "required": ["code_artifact_ref", "entry_point"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # Exclusive sandbox, no parallelism
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # TODO: Integrate athena.execution.sandbox_runtime for actual execution
        # Currently a stub implementation
        entry_point = input["entry_point"]

        return ToolResult(
            data={
                "entry_point": entry_point,
                "status": "executed",
                "logs": "[sandbox execution stub — needs sandbox_runtime integration]",
                "output_path": f"/tmp/{entry_point}_output",
            }
        )


# ── Submission Build Tool ──


class SubmissionBuildTool(BaseTool):
    """Package predictions into competition submission format."""

    spec = ToolSpec(
        name="submission_build",
        description=(
            "Package predictions into the required submission format. "
            "Takes the predictions file path and format spec, produces "
            "a submission.csv artifact."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "predictions_path": {
                    "type": "string",
                    "description": "Path to the predictions file from inference.",
                },
                "submission_format": {
                    "type": "string",
                    "description": "Description of submission format requirements.",
                },
            },
            "required": ["predictions_path", "submission_format"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        predictions_path = input["predictions_path"]
        submission_format = input["submission_format"]

        # LLM-driven formatting: if format doesn't match, LLM generates conversion code
        format_prompt = (
            f"Predictions file path: {predictions_path}\n"
            f"Required submission format: {submission_format}\n\n"
            f"Generate Python code to read the predictions and write a "
            f"submission.csv file matching the required format."
        )

        format_script = await single_turn_chat(
            system_prompt="You are a data formatting expert. Output only Python code.",
            user_prompt=format_prompt,
        )

        return ToolResult(
            data={
                "format_script": format_script,
                "predictions_path": predictions_path,
                "status": "submission_ready",
            }
        )

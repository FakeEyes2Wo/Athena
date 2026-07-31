"""基线构建器工具 — 方案设计、代码生成、沙箱执行、提交打包。

solution_design: 基于调研结果设计方案，附带 rubric 自检清单。
project_code_gen: 根据方案生成完整项目代码（model.py, dataset.py, train.py, infer.py, config）。
code_execute: 在沙箱中执行代码，捕获日志和结果。
submission_build: 按比赛格式打包预测结果。
"""

import json
from pathlib import Path

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat

# ── 系统提示词 ──

# 方案设计系统提示词：让 LLM 基于调研和分析设计竞赛方案，输出 JSON
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

# 代码生成系统提示词：让 LLM 根据方案生成完整的可运行 Python 项目
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


# ── 方案设计工具 ──


class SolutionDesignTool(BaseTool):
    """基于调研结果和数据分析设计竞赛方案，返回附带 rubric 自检清单的方案计划。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，方案设计的落盘目录
        self.output_dir = Path(work_root) / "solution_design"

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
        concurrency_safe=True,  # 仅调用 LLM，无外部副作用，可安全并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # 将输入组装为 LLM user prompt
        user_prompt = json.dumps(input, ensure_ascii=False)

        # 调用 LLM 生成方案设计，要求 JSON 格式输出
        result_text = await single_turn_chat(
            system_prompt=_SOLUTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        plan = json.loads(result_text)

        # 验证 rubric 必须存在 — 设计要求，缺失则返回失败
        if "rubric" not in plan or not plan["rubric"]:
            return ToolResult(
                success=False,
                data=None,
                error="Solution plan is missing required 'rubric' field. "
                      "The plan must include a self-check rubric.",
            )

        return ToolResult(data={"solution_plan": plan})


# ── 项目代码生成工具 ──


class ProjectCodeGenTool(BaseTool):
    """根据方案设计生成完整的可运行 Python 项目代码。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，项目代码的落盘目录
        self.output_dir = Path(work_root) / "project_code_gen"

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
        concurrency_safe=True,  # 仅调用 LLM，无外部副作用，可安全并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # 将方案引用和配置组装为 LLM user prompt
        user_prompt = json.dumps(input, ensure_ascii=False)

        # 调用 LLM 生成项目代码，不要求 JSON（输出为多文件代码）
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


# ── 代码执行工具 ──


class CodeExecuteTool(BaseTool):
    """在沙箱中执行训练或推理代码，捕获日志和输出路径。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，执行日志的落盘目录
        self.output_dir = Path(work_root) / "code_execute"

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
        concurrency_safe=False,  # 独占沙箱，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # TODO: 集成 athena.execution.sandbox_runtime 进行实际沙箱执行
        # 当前为占位实现，返回固定状态供 Agent 流程串联验证
        entry_point = input["entry_point"]

        return ToolResult(
            data={
                "entry_point": entry_point,
                "status": "executed",
                "logs": "[sandbox execution stub — needs sandbox_runtime integration]",
                "output_path": f"/tmp/{entry_point}_output",
            }
        )


# ── 提交打包工具 ──


class SubmissionBuildTool(BaseTool):
    """按比赛要求格式将预测结果打包为 submission.csv。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，提交打包产物的落盘目录
        self.output_dir = Path(work_root) / "submission_build"

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
        concurrency_safe=False,  # 可能写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        predictions_path = input["predictions_path"]
        submission_format = input["submission_format"]

        # LLM 驱动格式化：如果格式不匹配，让 LLM 生成转换代码
        format_prompt = (
            f"Predictions file path: {predictions_path}\n"
            f"Required submission format: {submission_format}\n\n"
            f"Generate Python code to read the predictions and write a "
            f"submission.csv file matching the required format."
        )

        # 调用 LLM 生成格式转换脚本
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

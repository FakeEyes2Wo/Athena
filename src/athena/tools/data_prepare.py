"""数据准备工具 — EDA 分析和数据清洗代码生成。

data_analyze: 对数据集进行探索性数据分析，生成 EDA 报告。
data_clean_code_gen: 基于 EDA 报告生成清洗脚本，产出 cleaned data DataCard。
"""

import json

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat

# ── 系统提示词 ──

# EDA 系统提示词：让 LLM 分析数据集的各项特征
_EDA_SYSTEM_PROMPT = """\
You are a data analyst. Given a dataset schema summary, produce an
Exploratory Data Analysis (EDA) report as JSON.

Output JSON with these fields:
- distributions: per-column distribution description
- missing_values: columns with missing values and counts
- outliers: columns with outlier issues and description
- correlations: key pairwise correlations (if tabular)
- class_balance: target variable distribution (if classification)

Be quantitative where possible. Note data quality issues that need cleaning.
"""

# 清洗代码生成提示词：让 LLM 基于 EDA 报告生成可运行的清洗脚本
_CLEAN_SYSTEM_PROMPT = """\
You are a data engineer. Given an EDA report, generate a Python cleaning
script that:

1. Handles missing values (drop or impute with justification)
2. Removes or caps outliers
3. Encodes categorical variables
4. Normalizes/scales numeric features if needed

Output the COMPLETE runnable Python script as a code block. The script
should read data from INPUT_PATH, clean it, and write to OUTPUT_PATH.
"""


# ── EDA 分析工具 ──

class DataAnalyzeTool(BaseTool):
    """对数据集进行探索性数据分析（EDA），生成 JSON 格式的 EDA 报告。"""

    spec = ToolSpec(
        name="data_analyze",
        description=(
            "Perform exploratory data analysis (EDA) on datasets given "
            "their DataCard references. Returns an EDA report covering "
            "distributions, missing values, outliers, correlations, and "
            "class balance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "data_card_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of DataCard artifact references.",
                }
            },
            "required": ["data_card_refs"],
            "additionalProperties": False,
        },
        concurrency_safe=True,  # 仅调用 LLM，无外部副作用，可安全并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        refs = input["data_card_refs"]
        if not refs:
            return ToolResult(
                success=False, error="At least one DataCard ref is required"
            )

        # 从 DataCard 中收集 schema 信息，构建分析 prompt
        # 生产环境中应从 ArtifactStore 读取实际 schema
        schema_summaries = []
        for ref in refs:
            schema_summaries.append(f"Dataset ref: {ref}")

        user_prompt = (
            "Analyze the following datasets and produce an EDA report:\n\n"
            + "\n".join(schema_summaries)
        )

        # 调用 LLM 生成 EDA 报告，要求 JSON 格式输出
        eda_result = await single_turn_chat(
            system_prompt=_EDA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        # EDA 报告作为解析后的 JSON artifact 返回
        return ToolResult(
            data={
                "eda_report": json.loads(eda_result),
                "data_card_refs": refs,
            }
        )


# ── 数据清洗代码生成工具 ──

class DataCleanCodeGenTool(BaseTool):
    """基于 EDA 报告生成数据清洗 Python 脚本，返回脚本内容和元信息。"""

    spec = ToolSpec(
        name="data_clean_code_gen",
        description=(
            "Generate a data cleaning Python script based on an EDA report. "
            "The script handles missing values, outliers, encoding, and "
            "normalization. Returns the script and cleaned data path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "eda_report_ref": {
                    "type": "string",
                    "description": "Artifact reference to the EDA report JSON.",
                },
                "data_card_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of DataCard artifact references.",
                },
            },
            "required": ["eda_report_ref", "data_card_refs"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 生成脚本后可能执行写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # 基于 EDA 报告用 LLM 生成清洗脚本
        eda_ref = input["eda_report_ref"]
        data_refs = input["data_card_refs"]

        user_prompt = (
            f"EDA report reference: {eda_ref}\n"
            f"Dataset references: {', '.join(data_refs)}\n\n"
            f"Generate a Python cleaning script for these datasets."
        )

        # 调用 LLM 生成清洗代码，不要求 JSON（输出为 Python 脚本）
        clean_script = await single_turn_chat(
            system_prompt=_CLEAN_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        return ToolResult(
            data={
                "clean_script": clean_script,
                "eda_report_ref": eda_ref,
                "data_card_refs": data_refs,
                "status": "script_generated",
            }
        )

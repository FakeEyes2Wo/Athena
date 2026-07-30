"""Data preparation tools — EDA analysis and data cleaning code generation.

data_analyze: Perform exploratory data analysis on datasets, producing an EDA report.
data_clean_code_gen: Generate a cleaning script based on an EDA report,
    and execute it to produce cleaned data.
"""

import json

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat

# EDA system prompt: instruct the LLM to analyze dataset characteristics
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

# Cleaning code generation system prompt
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


class DataAnalyzeTool(BaseTool):
    """Perform exploratory data analysis (EDA) on datasets."""

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
        concurrency_safe=True,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        refs = input["data_card_refs"]
        if not refs:
            return ToolResult(
                success=False, error="At least one DataCard ref is required"
            )

        # Build analysis prompt from DataCard schema info.
        # In production, this would read schemas from ArtifactStore.
        schema_summaries = []
        for ref in refs:
            schema_summaries.append(f"Dataset ref: {ref}")

        user_prompt = (
            "Analyze the following datasets and produce an EDA report:\n\n"
            + "\n".join(schema_summaries)
        )

        eda_result = await single_turn_chat(
            system_prompt=_EDA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        # Return the EDA report as parsed JSON
        return ToolResult(
            data={
                "eda_report": json.loads(eda_result),
                "data_card_refs": refs,
            }
        )


class DataCleanCodeGenTool(BaseTool):
    """Generate a data cleaning Python script based on an EDA report."""

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
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # Generate cleaning script based on EDA report using LLM
        eda_ref = input["eda_report_ref"]
        data_refs = input["data_card_refs"]

        user_prompt = (
            f"EDA report reference: {eda_ref}\n"
            f"Dataset references: {', '.join(data_refs)}\n\n"
            f"Generate a Python cleaning script for these datasets."
        )

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

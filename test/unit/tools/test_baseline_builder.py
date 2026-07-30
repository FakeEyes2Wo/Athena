import json
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.baseline_builder import (
    CodeExecuteTool,
    ProjectCodeGenTool,
    SolutionDesignTool,
    SubmissionBuildTool,
)


@pytest.fixture
def mock_llm():
    """Mock LLM returning a fixed solution plan with rubric."""
    with patch("athena.tools.baseline_builder.single_turn_chat") as mock:
        mock.return_value = json.dumps({
            "model_selection": "XGBoost with default hyperparameters",
            "pipeline_structure": "Feature engineering -> Train -> Predict",
            "training_strategy": "5-fold cross-validation",
            "loss_metric": "log_loss",
            "rubric": {
                "clarity": "Is the solution approach clearly explained?",
                "feasibility": "Can this be implemented and run within constraints?",
            },
        })
        yield mock


@pytest.mark.asyncio
async def test_solution_design_returns_plan(mock_llm):
    tool = SolutionDesignTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_metadata={"task_type": "binary_classification", "data_type": "tabular"},
        eda_report_ref="sha256:aaa",
        research_refs=["sha256:bbb"],
        model_candidates=[{"id": "xgboost"}],
    )
    assert result.success is True
    plan = result.data["solution_plan"]
    assert plan["model_selection"] == "XGBoost with default hyperparameters"
    # Rubric must be present (design requirement)
    assert "rubric" in plan


@pytest.mark.asyncio
async def test_solution_design_without_rubric_rejected(mock_llm):
    """If LLM returns a plan with missing rubric, tool must report failure."""
    mock_llm.return_value = json.dumps({
        "model_selection": "some model",
        "pipeline_structure": "some pipeline",
        "training_strategy": "some strategy",
        "loss_metric": "some metric",
        # rubric intentionally omitted
    })
    tool = SolutionDesignTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_metadata={"task_type": "test"},
        eda_report_ref="sha256:aaa",
        research_refs=[],
        model_candidates=[],
    )
    assert result.success is False
    assert "rubric" in str(result.error).lower()


@pytest.fixture
def mock_llm_code():
    """Mock LLM returning a fixed code project."""
    with patch("athena.tools.baseline_builder.single_turn_chat") as mock:
        mock.return_value = (
            "# model.py\n"
            "import torch\n\n"
            "class Model(torch.nn.Module):\n"
            "    pass\n"
        )
        yield mock


@pytest.mark.asyncio
async def test_project_code_gen_returns_code(mock_llm_code):
    tool = ProjectCodeGenTool()
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        solution_plan_ref="sha256:ccc",
        data_card_refs=["sha256:ddd"],
        submission_format="CSV with id, prediction columns",
    )
    assert result.success is True
    assert "code" in result.data
    assert result.data["status"] == "code_generated"


@pytest.mark.asyncio
async def test_code_execute_returns_status():
    tool = CodeExecuteTool()
    ctx = ToolContext("test", "call-4", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        code_artifact_ref="sha256:eee",
        entry_point="train",
    )
    assert result.success is True
    assert result.data["entry_point"] == "train"
    assert result.data["status"] == "executed"


@pytest.fixture
def mock_llm_format():
    """Mock LLM returning a format script."""
    with patch("athena.tools.baseline_builder.single_turn_chat") as mock:
        mock.return_value = (
            "import pandas as pd\n"
            "df = pd.read_csv('preds.csv')\n"
            "df.to_csv('submission.csv', index=False)\n"
        )
        yield mock


@pytest.mark.asyncio
async def test_submission_build_returns_format_script(mock_llm_format):
    tool = SubmissionBuildTool()
    ctx = ToolContext("test", "call-5", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        predictions_path="/tmp/predictions.csv",
        submission_format="CSV with columns: id, target",
    )
    assert result.success is True
    assert "format_script" in result.data
    assert result.data["status"] == "submission_ready"

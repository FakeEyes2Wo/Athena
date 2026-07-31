import json
import tempfile
from pathlib import Path
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
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


# ── SolutionDesignTool ──


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
async def test_solution_design_returns_plan(mock_llm, tmp_work_root):
    tool = SolutionDesignTool(work_root=tmp_work_root)
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
    assert "rubric" in plan

    # 验证落盘
    assert "output_dir" in result.data
    plan_file = Path(result.data["output_dir"]) / "solution_plan.json"
    assert plan_file.exists()
    on_disk = json.loads(plan_file.read_text(encoding="utf-8"))
    assert on_disk["model_selection"] == "XGBoost with default hyperparameters"


@pytest.mark.asyncio
async def test_solution_design_without_rubric_rejected(mock_llm, tmp_work_root):
    mock_llm.return_value = json.dumps({
        "model_selection": "some model",
        "pipeline_structure": "some pipeline",
        "training_strategy": "some strategy",
        "loss_metric": "some metric",
    })
    tool = SolutionDesignTool(work_root=tmp_work_root)
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


# ── ProjectCodeGenTool ──


@pytest.fixture
def mock_llm_code():
    """Mock LLM returning a complete multi-file code project."""
    with patch("athena.tools.baseline_builder.single_turn_chat") as mock:
        mock.return_value = (
            "### model.py\n"
            "```python\n"
            "import torch\n"
            "class Model(torch.nn.Module):\n"
            "    def __init__(self):\n"
            "        super().__init__()\n"
            "        self.fc = torch.nn.Linear(10, 1)\n"
            "    def forward(self, x):\n"
            "        return self.fc(x)\n"
            "```\n\n"
            "### dataset.py\n"
            "```python\n"
            "from torch.utils.data import Dataset\n"
            "class MyDataset(Dataset):\n"
            "    def __len__(self): return 100\n"
            "    def __getitem__(self, i): return torch.randn(10), 0.0\n"
            "```\n\n"
            "### train.py\n"
            "```python\n"
            "print('training...')\n"
            "```\n\n"
            "### infer.py\n"
            "```python\n"
            "print('inferring...')\n"
            "```\n\n"
            "### config.yaml\n"
            "```\n"
            "lr: 0.001\n"
            "epochs: 10\n"
            "```\n"
        )
        yield mock


@pytest.mark.asyncio
async def test_project_code_gen_writes_files(mock_llm_code, tmp_work_root):
    tool = ProjectCodeGenTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        solution_plan_ref="sha256:ccc",
        data_card_refs=["sha256:ddd"],
        submission_format="CSV with id, prediction columns",
    )
    assert result.success is True
    assert result.data["status"] == "code_generated"

    # 验证 5 个文件全部落盘
    output_dir = Path(result.data["output_dir"])
    expected = {"model.py", "dataset.py", "train.py", "infer.py", "config.yaml"}
    assert set(result.data["files"]) == expected
    for fname in expected:
        fpath = output_dir / fname
        assert fpath.exists(), f"Missing file: {fname}"
        assert len(fpath.read_text(encoding="utf-8")) > 0


@pytest.mark.asyncio
async def test_project_code_gen_missing_file_fails(tmp_work_root):
    """LLM 响应不包含全部 5 个文件时返回 success=False。"""
    with patch("athena.tools.baseline_builder.single_turn_chat") as mock:
        mock.return_value = (
            "### model.py\n```python\nprint('only model')\n```\n"
        )
        tool = ProjectCodeGenTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-3b", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            solution_plan_ref="sha256:ccc",
            data_card_refs=["sha256:ddd"],
            submission_format="CSV",
        )
        assert result.success is False
        assert "Missing required files" in result.error


# ── CodeExecuteTool ──


@pytest.mark.asyncio
async def test_code_execute_writes_log(tmp_work_root):
    tool = CodeExecuteTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-4", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        code_artifact_ref="sha256:eee",
        entry_point="train",
    )
    assert result.success is True
    assert result.data["entry_point"] == "train"
    assert result.data["status"] == "executed"

    # 验证日志落盘
    log_file = Path(result.data["output_dir"]) / "train_log.txt"
    assert log_file.exists()
    assert "sandbox execution stub" in log_file.read_text(encoding="utf-8")


# ── SubmissionBuildTool ──


@pytest.fixture
def mock_llm_format():
    """Mock LLM returning a format script inside a code block."""
    with patch("athena.tools.baseline_builder.single_turn_chat") as mock:
        mock.return_value = (
            "```python\n"
            "import pandas as pd\n"
            "df = pd.read_csv('preds.csv')\n"
            "df.to_csv('submission.csv', index=False)\n"
            "```\n"
        )
        yield mock


@pytest.mark.asyncio
async def test_submission_build_writes_script(mock_llm_format, tmp_work_root):
    tool = SubmissionBuildTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-5", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        predictions_path="/tmp/predictions.csv",
        submission_format="CSV with columns: id, target",
    )
    assert result.success is True
    assert result.data["status"] == "submission_ready"

    # 验证脚本落盘（只含代码，不含 markdown 标记）
    script_file = Path(result.data["output_dir"]) / "format_script.py"
    assert script_file.exists()
    content = script_file.read_text(encoding="utf-8")
    assert "import pandas as pd" in content
    assert "```" not in content  # 代码块标记已被去除

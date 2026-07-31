# 工具输出直接落盘 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 TaskUnderstandAgent 的 13 个工具在执行阶段直接将产物写入文件系统，不再依赖对话历史提取。

**Architecture:** 每个工具通过构造器接收 `work_root`，在 `execute()` 中写入 `{work_root}/{tool_name}/` 子目录。不改 `ToolContext`、`BaseTool`、`Agent`、`ThreadRuntime`。共享的代码解析逻辑（从 LLM 文本提取代码块、解析多文件项目）抽入新文件 `_code_utils.py`。

**Tech Stack:** Python 标准库 (`json`, `pathlib.Path`, `re`)，已有依赖 (`huggingface_hub`, `pytest`)

## Global Constraints

- 不改 `core/tool.py`、`core/tool_types.py`、`core/agent/agent.py`、`app_server/thread_runtime.py`
- 所有文件写入错误通过 `ToolResult(success=False)` 返回，不抛异常
- JSON 输出工具直接 `json.dump`；代码输出工具从 LLM 文本解析
- 代码解析失败（无代码块/缺文件）→ `ToolResult(success=False)`
- 同一工具多次调用覆盖前一次输出

---

## File Structure

| 文件 | 角色 | 操作 |
|---|---|---|
| `src/athena/tools/_code_utils.py` | 共享的 LLM 响应解析函数 | **Create** |
| `src/athena/agents/competition/task_understand_agent.py` | Agent 入口，新增 `work_root` 参数 | Modify |
| `src/athena/tools/baseline_builder.py` | 4 个工具加 `__init__` + 落盘逻辑 | Modify |
| `src/athena/tools/data_prepare.py` | 2 个工具加 `__init__` + 落盘逻辑 | Modify |
| `src/athena/tools/kaggle_search.py` | 3 个工具加 `__init__` + 落盘逻辑 | Modify |
| `src/athena/tools/hf_dataset.py` | 2 个工具加 `__init__` + 落盘逻辑 | Modify |
| `src/athena/tools/hf_model.py` | 2 个工具加 `__init__` + 落盘逻辑 | Modify |
| `test/unit/tools/test_baseline_builder.py` | 新增落盘验证测试 | Modify |
| `test/unit/tools/test_data_prepare.py` | 新增落盘验证测试 | Modify |
| `test/unit/tools/test_kaggle_search.py` | 新增落盘验证测试 | Modify |
| `test/unit/tools/test_hf_dataset.py` | 新增落盘验证测试 | Modify |
| `test/unit/tools/test_hf_model.py` | 新增落盘验证测试 | Modify |

---

### Task 1: 共享代码解析工具 `_code_utils.py` + Agent 入口改造

**Files:**
- Create: `src/athena/tools/_code_utils.py`
- Modify: `src/athena/agents/competition/task_understand_agent.py:76-140`

**Interfaces:**
- Produces:
  - `_extract_code_block(text: str) -> str | None` — 从 Markdown 文本提取第一个 Python 代码块
  - `_parse_code_files(text: str) -> dict[str, str]` — 从 LLM 文本解析多文件项目，缺文件抛 `ValueError`
  - `build_task_understand_agent(model, client=None, *, work_root="work", max_turns=30, ...) -> Agent` — 新增 `work_root` 参数

- [ ] **Step 1: 编写 `_code_utils.py` 及测试**

```python
"""共享的 LLM 响应解析工具。

用于从自由文本 LLM 响应中提取代码块和结构化文件内容。
供 data_prepare、baseline_builder 等工具使用。
"""

import re

# project_code_gen 必须产出的 5 个文件
_REQUIRED_CODE_FILES = {"model.py", "dataset.py", "train.py", "infer.py", "config.yaml"}


def extract_code_block(text: str) -> str | None:
    """从 LLM 文本响应中提取第一个 Python 代码块。

    优先匹配 ```python ... ```，回退到任意 ``` ... ```。
    返回代码内容字符串，无匹配时返回 None。
    """
    # 优先：python 语言标记的代码块
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 回退：任意代码块
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def parse_code_files(text: str) -> dict[str, str]:
    """从 LLM 文本响应中解析多文件项目代码。

    支持的格式：
    - ``### filename.py`` 标题后跟代码块
    - ``## filename.py`` 标题后跟代码块

    必须包含全部 5 个文件：model.py, dataset.py, train.py, infer.py, config.yaml。
    缺任何文件抛出 ValueError。

    Returns:
        {filename: code_content} 字典。
    """
    files: dict[str, str] = {}

    # 按 Markdown 标题分割：### filename.ext 或 ## filename.ext
    parts = re.split(r"\n(?=##?#?\s+\S+\.(?:py|yaml|yml)\s*\n)", text)

    for part in parts:
        m = re.match(r"##?#?\s+(\S+\.(?:py|yaml|yml))\s*\n", part)
        if not m:
            continue
        fname = m.group(1)
        body = part[m.end():]
        code = extract_code_block(body)
        files[fname] = code if code else body.strip()

    missing = _REQUIRED_CODE_FILES - set(files.keys())
    if missing:
        raise ValueError(
            f"Missing required files: {', '.join(sorted(missing))}"
        )
    return files
```

- [ ] **Step 2: 运行测试验证 `_code_utils` 逻辑**

```bash
cd c:/Users/YeBai/Athena && uv run python -c "
from src.athena.tools._code_utils import extract_code_block, parse_code_files

# Test extract_code_block
text = 'Some text\n```python\nprint(1)\n```\nmore'
assert extract_code_block(text) == 'print(1)'
assert extract_code_block('no code here') is None

# Test parse_code_files
multi = '''### model.py
```python
import torch
class Model(torch.nn.Module):
    pass
```

### dataset.py
```python
from torch.utils.data import Dataset
class MyDataset(Dataset):
    pass
```

### train.py
```python
print('train')
```

### infer.py
```python
print('infer')
```

### config.yaml
```yaml
lr: 0.001
```
'''
files = parse_code_files(multi)
assert set(files.keys()) == {'model.py', 'dataset.py', 'train.py', 'infer.py', 'config.yaml'}
assert 'class Model' in files['model.py']
print('All _code_utils tests passed')
"
```

- [ ] **Step 3: 修改 `build_task_understand_agent()` 添加 `work_root` 参数**

在 [task_understand_agent.py:76-78](src/athena/agents/competition/task_understand_agent.py#L76-L78)，修改函数签名并传给各工具：

```python
def build_task_understand_agent(
    model: str,
    client: "AsyncOpenAI | None" = None,
    *,
    work_root: str = "work",   # 新增：产物输出根目录
    max_turns: int = 30,
    max_tokens: int = 8192,
    temperature: float = 0.1,
) -> Agent:
    """构建 TaskUnderstandAgent，注册所有竞赛工具。

    Args:
        model: LLM 模型名称（如 "deepseek-v4-flash"）。
        client: OpenAI 兼容的异步客户端。
        work_root: 工具产物输出根目录，每个工具在下方创建子目录。
        max_turns: Agent loop 最大轮次（竞赛流程需要较多轮次）。
        max_tokens: 每次 LLM 请求的最大 token 数。
        temperature: LLM 温度参数。

    Returns:
        配置完成的 Agent 实例，可直接用于 ThreadRuntime。
    """
    tools = ToolRegistry()

    # ── 搜索 & 理解层 (3 工具) ──
    tools.register(KaggleCompetitionSearchTool(work_root=work_root))
    tools.register(KaggleDiscussionSearchTool(work_root=work_root))
    tools.register(KaggleDatasetDownloadTool(work_root=work_root))

    # ── 数据获取层 —— HF 数据集 (2 工具) ──
    tools.register(HFDatasetSearchTool(work_root=work_root))
    tools.register(HFDatasetDownloadTool(work_root=work_root))

    # ── 数据获取层 —— HF 模型 (2 工具) ──
    tools.register(HFModelSearchTool(work_root=work_root))
    tools.register(HFModelDownloadTool(work_root=work_root))

    # ── 数据准备层 (2 工具) ──
    tools.register(DataAnalyzeTool(work_root=work_root))
    tools.register(DataCleanCodeGenTool(work_root=work_root))

    # ── 建模 & 提交层 (4 工具) ──
    tools.register(SolutionDesignTool(work_root=work_root))
    tools.register(ProjectCodeGenTool(work_root=work_root))
    tools.register(CodeExecuteTool(work_root=work_root))
    tools.register(SubmissionBuildTool(work_root=work_root))

    # Guard：确保全部 13 个工具已注册
    assert len(tools) == 13, (
        f"Expected 13 competition tools, got {len(tools)}"
    )

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=_COMPETITION_SYSTEM_PROMPT,
        client=client,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
        name="TaskUnderstandAgent",
        description=(
            "Autonomous Kaggle competition agent that researches, "
            "downloads data, builds baselines, and produces submissions."
        ),
    )
```

- [ ] **Step 4: 验证导入正确**

```bash
cd c:/Users/YeBai/Athena && uv run python -c "
from athena.agents.competition.task_understand_agent import build_task_understand_agent
agent = build_task_understand_agent(model='test', work_root='/tmp/test_work')
print(f'Agent created: {agent.name}, tools: {len(agent.config.tools)}')
"
```

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/_code_utils.py src/athena/agents/competition/task_understand_agent.py
git commit -m "feat: add _code_utils helpers and work_root param to build_task_understand_agent"
```

---

### Task 2: baseline_builder.py — 4 个工具落盘

**Files:**
- Modify: `src/athena/tools/baseline_builder.py`
- Modify: `test/unit/tools/test_baseline_builder.py`

**Interfaces:**
- Consumes: `extract_code_block` from `athena.tools._code_utils`, `parse_code_files` from `athena.tools._code_utils`
- Produces: `SolutionDesignTool(work_root)`, `ProjectCodeGenTool(work_root)`, `CodeExecuteTool(work_root)`, `SubmissionBuildTool(work_root)` — 每个构造后 `self.output_dir` 可用

- [ ] **Step 1: 修改 `SolutionDesignTool` 加 `__init__` 和落盘**

在 [baseline_builder.py:51](src/athena/tools/baseline_builder.py#L51) 的 `SolutionDesignTool` 类中添加：

```python
import json
from pathlib import Path

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat


class SolutionDesignTool(BaseTool):
    """基于调研结果和数据分析设计竞赛方案，返回附带 rubric 自检清单的方案计划。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "solution_design"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        user_prompt = json.dumps(input, ensure_ascii=False)

        result_text = await single_turn_chat(
            system_prompt=_SOLUTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        plan = json.loads(result_text)

        if "rubric" not in plan or not plan["rubric"]:
            return ToolResult(
                success=False,
                data=None,
                error="Solution plan is missing required 'rubric' field. "
                      "The plan must include a self-check rubric.",
            )

        # 落盘 solution_plan.json
        self.output_dir.mkdir(parents=True, exist_ok=True)
        plan_path = self.output_dir / "solution_plan.json"
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ToolResult(data={
            "solution_plan": plan,
            "output_dir": str(self.output_dir),
        })
```

- [ ] **Step 2: 修改 `ProjectCodeGenTool` 加 `__init__` 和多文件落盘**

在 [baseline_builder.py:117](src/athena/tools/baseline_builder.py#L117) 的 `ProjectCodeGenTool` 类中：

```python
class ProjectCodeGenTool(BaseTool):
    """根据方案设计生成完整的可运行 Python 项目代码。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "project_code_gen"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        from athena.tools._code_utils import parse_code_files

        user_prompt = json.dumps(input, ensure_ascii=False)

        code_text = await single_turn_chat(
            system_prompt=_CODE_GEN_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        # 从 LLM 响应中解析各文件
        try:
            files = parse_code_files(code_text)
        except ValueError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                data={"raw_response": code_text[:500]},
            )

        # 落盘所有文件
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            (self.output_dir / fname).write_text(content, encoding="utf-8")

        return ToolResult(
            data={
                "files": sorted(files.keys()),
                "output_dir": str(self.output_dir),
                "solution_plan_ref": input["solution_plan_ref"],
                "status": "code_generated",
            }
        )
```

- [ ] **Step 3: 修改 `CodeExecuteTool` 加 `__init__` 和落盘**

在 [baseline_builder.py:175](src/athena/tools/baseline_builder.py#L175) 的 `CodeExecuteTool` 类中：

```python
class CodeExecuteTool(BaseTool):
    """在沙箱中执行训练或推理代码，捕获日志和输出路径。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "code_execute"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        entry_point = input["entry_point"]

        # TODO: 集成 athena.execution.sandbox_runtime 进行实际沙箱执行
        log_content = (
            f"[sandbox execution stub — entry_point={entry_point}]\n"
            f"code_artifact_ref: {input['code_artifact_ref']}\n"
            f"needs sandbox_runtime integration\n"
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        log_name = f"{entry_point}_log.txt"
        (self.output_dir / log_name).write_text(log_content, encoding="utf-8")
        (self.output_dir / f"{entry_point}_output").mkdir(parents=True, exist_ok=True)

        return ToolResult(
            data={
                "entry_point": entry_point,
                "status": "executed",
                "logs": log_content,
                "output_dir": str(self.output_dir),
                "output_path": str(self.output_dir / f"{entry_point}_output"),
            }
        )
```

- [ ] **Step 4: 修改 `SubmissionBuildTool` 加 `__init__` 和落盘**

在 [baseline_builder.py:222](src/athena/tools/baseline_builder.py#L222) 的 `SubmissionBuildTool` 类中：

```python
class SubmissionBuildTool(BaseTool):
    """按比赛要求格式将预测结果打包为 submission.csv。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "submission_build"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        from athena.tools._code_utils import extract_code_block

        predictions_path = input["predictions_path"]
        submission_format = input["submission_format"]

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

        # 提取代码块并落盘
        self.output_dir.mkdir(parents=True, exist_ok=True)
        script_content = extract_code_block(format_script)
        if script_content is None:
            script_content = format_script  # 没有代码块标记则全量保存
        (self.output_dir / "format_script.py").write_text(
            script_content, encoding="utf-8"
        )

        return ToolResult(
            data={
                "format_script": format_script,
                "predictions_path": predictions_path,
                "status": "submission_ready",
                "output_dir": str(self.output_dir),
            }
        )
```

- [ ] **Step 5: 更新测试 — 添加落盘验证**

在 `test/unit/tools/test_baseline_builder.py` 中为每个工具新增 `output_dir` 验证：

```python
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
            "```yaml\n"
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
```

- [ ] **Step 6: 运行 baseline_builder 测试**

```bash
cd c:/Users/YeBai/Athena && uv run pytest test/unit/tools/test_baseline_builder.py -v
```
Expected: 全部 6 个测试 PASS

- [ ] **Step 7: Commit**

```bash
git add src/athena/tools/baseline_builder.py test/unit/tools/test_baseline_builder.py
git commit -m "feat: baseline_builder tools write outputs to disk"
```

---

### Task 3: data_prepare.py — 2 个工具落盘

**Files:**
- Modify: `src/athena/tools/data_prepare.py`
- Modify: `test/unit/tools/test_data_prepare.py`

**Interfaces:**
- Consumes: `extract_code_block` from `athena.tools._code_utils`
- Produces: `DataAnalyzeTool(work_root)`, `DataCleanCodeGenTool(work_root)`

- [ ] **Step 1: 修改 `DataAnalyzeTool` 加 `__init__` 和落盘**

在 [data_prepare.py:47](src/athena/tools/data_prepare.py#L47)：

```python
import json
from pathlib import Path

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat


class DataAnalyzeTool(BaseTool):
    """对数据集进行探索性数据分析（EDA），生成 JSON 格式的 EDA 报告。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "data_analyze"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        refs = input["data_card_refs"]
        if not refs:
            return ToolResult(
                success=False, error="At least one DataCard ref is required"
            )

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

        report = json.loads(eda_result)

        # 落盘 eda_report.json
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "eda_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ToolResult(
            data={
                "eda_report": report,
                "data_card_refs": refs,
                "output_dir": str(self.output_dir),
            }
        )
```

- [ ] **Step 2: 修改 `DataCleanCodeGenTool` 加 `__init__` 和落盘**

在 [data_prepare.py:109](src/athena/tools/data_prepare.py#L109)：

```python
class DataCleanCodeGenTool(BaseTool):
    """基于 EDA 报告生成数据清洗 Python 脚本，返回脚本内容和元信息。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "data_clean_code_gen"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        from athena.tools._code_utils import extract_code_block

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

        # 提取代码块并落盘
        self.output_dir.mkdir(parents=True, exist_ok=True)
        code = extract_code_block(clean_script)
        if code is None:
            return ToolResult(
                success=False,
                error="No code block found in LLM response",
                data={"raw_response": clean_script[:500]},
            )
        (self.output_dir / "clean_script.py").write_text(code, encoding="utf-8")

        return ToolResult(
            data={
                "clean_script": clean_script,
                "eda_report_ref": eda_ref,
                "data_card_refs": data_refs,
                "status": "script_generated",
                "output_dir": str(self.output_dir),
            }
        )
```

- [ ] **Step 3: 更新测试**

在 `test/unit/tools/test_data_prepare.py` 中：

```python
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool


@pytest.fixture
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def mock_llm():
    """Mock LLM returning a fixed EDA report."""
    with patch("athena.tools.data_prepare.single_turn_chat") as mock:
        mock.return_value = json.dumps({
            "distributions": {"PassengerId": "uniform", "Survived": "binary"},
            "missing_values": {"Age": 177},
            "outliers": {"Fare": "right-skewed with extreme values"},
            "correlations": {"Pclass-Survived": -0.34},
            "class_balance": {"Survived": {"0": 549, "1": 342}},
        })
        yield mock


@pytest.mark.asyncio
async def test_data_analyze_returns_eda_report(mock_llm, tmp_work_root):
    tool = DataAnalyzeTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, data_card_refs=["sha256:aaaa"])
    assert result.success is True
    assert "eda_report" in result.data

    # 验证落盘
    report_file = Path(result.data["output_dir"]) / "eda_report.json"
    assert report_file.exists()
    on_disk = json.loads(report_file.read_text(encoding="utf-8"))
    assert on_disk["class_balance"]["Survived"]["0"] == 549


@pytest.mark.asyncio
async def test_data_analyze_empty_refs_errors(tmp_work_root):
    tool = DataAnalyzeTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, data_card_refs=[])
    assert result.success is False


@pytest.fixture
def mock_llm_clean():
    """Mock LLM returning a cleaning script inside a code block."""
    with patch("athena.tools.data_prepare.single_turn_chat") as mock:
        mock.return_value = (
            "```python\n"
            "import pandas as pd\n"
            "df = pd.read_csv(INPUT_PATH)\n"
            "df.fillna(0, inplace=True)\n"
            "df.to_csv(OUTPUT_PATH, index=False)\n"
            "```\n"
        )
        yield mock


@pytest.mark.asyncio
async def test_data_clean_code_gen_writes_script(mock_llm_clean, tmp_work_root):
    tool = DataCleanCodeGenTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        eda_report_ref="sha256:bbbb",
        data_card_refs=["sha256:aaaa"],
    )
    assert result.success is True
    assert result.data["status"] == "script_generated"

    # 验证脚本落盘（不含 markdown 标记）
    script_file = Path(result.data["output_dir"]) / "clean_script.py"
    assert script_file.exists()
    content = script_file.read_text(encoding="utf-8")
    assert "import pandas as pd" in content
    assert "```" not in content


@pytest.mark.asyncio
async def test_data_clean_code_gen_no_code_block_fails(tmp_work_root):
    """LLM 响应无代码块时返回 success=False。"""
    with patch("athena.tools.data_prepare.single_turn_chat") as mock:
        mock.return_value = "Here is some text without any code block."
        tool = DataCleanCodeGenTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-4", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            eda_report_ref="sha256:bbbb",
            data_card_refs=["sha256:aaaa"],
        )
        assert result.success is False
        assert "No code block" in result.error
```

- [ ] **Step 4: 运行 data_prepare 测试**

```bash
cd c:/Users/YeBai/Athena && uv run pytest test/unit/tools/test_data_prepare.py -v
```
Expected: 全部 4 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/data_prepare.py test/unit/tools/test_data_prepare.py
git commit -m "feat: data_prepare tools write outputs to disk"
```

---

### Task 4: kaggle_search.py — 3 个工具落盘

**Files:**
- Modify: `src/athena/tools/kaggle_search.py`
- Modify: `test/unit/tools/test_kaggle_search.py`

**Interfaces:**
- Produces: `KaggleCompetitionSearchTool(work_root)`, `KaggleDiscussionSearchTool(work_root)`, `KaggleDatasetDownloadTool(work_root)`

- [ ] **Step 1: 修改 `KaggleCompetitionSearchTool` 加 `__init__` 和落盘**

在 [kaggle_search.py:28](src/athena/tools/kaggle_search.py#L28)：

```python
import json
from pathlib import Path
from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.workflows.prepare.task_parser import parse_competition_info


class KaggleCompetitionSearchTool(BaseTool):
    """搜索 Kaggle 比赛基本信息：描述、评价指标、数据格式、提交要求。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "kaggle_competition_search"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_url = input["competition_url"]

        mcp = _get_kaggle_mcp_client()
        raw_html = await mcp.call_tool(
            "fetch_competition_page", {"url": competition_url}
        )

        metadata = await parse_competition_info(raw_html, competition_url)

        data = {
            "task_type": metadata.task_type,
            "data_type": metadata.data_type,
            "target_vars": metadata.target_vars,
            "primary_metric": {
                "name": metadata.primary_metric.name,
                "direction": metadata.primary_metric.direction,
            },
            "constraints": metadata.constraints,
            "source_url": competition_url,
            "description_text": raw_html,
        }

        # 落盘 competition_info.json
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "competition_info.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ToolResult(data={**data, "output_dir": str(self.output_dir)})
```

- [ ] **Step 2: 修改 `KaggleDiscussionSearchTool` 加 `__init__` 和落盘**

在 [kaggle_search.py:82](src/athena/tools/kaggle_search.py#L82)：

```python
class KaggleDiscussionSearchTool(BaseTool):
    """搜索 Kaggle Discussion/Notebook 中的 Top 方案思路。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "kaggle_discussion_search"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_url = input["competition_url"]
        top_k = input.get("top_k", 10)

        mcp = _get_kaggle_mcp_client()
        raw = await mcp.call_tool(
            "search_discussions",
            {"url": competition_url, "top_k": top_k},
        )
        discussions = json.loads(raw) if isinstance(raw, str) else raw

        summaries = []
        for d in discussions.get("discussions", []):
            summaries.append({
                "title": d.get("title", ""),
                "author": d.get("author", ""),
                "approach_summary": d.get("summary", ""),
                "score": d.get("score"),
                "url": d.get("url", ""),
            })

        data = {
            "discussion_count": len(summaries),
            "discussions": summaries,
        }

        # 落盘 discussions.json
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "discussions.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ToolResult(data={**data, "output_dir": str(self.output_dir)})
```

- [ ] **Step 3: 修改 `KaggleDatasetDownloadTool` 加 `__init__` 和落盘**

在 [kaggle_search.py:143](src/athena/tools/kaggle_search.py#L143)：

```python
class KaggleDatasetDownloadTool(BaseTool):
    """通过 Kaggle MCP 下载比赛数据集并生成 DataCard。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "kaggle_dataset_download"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_ref = input["competition_ref"]
        # 使用构造器注入的 output_dir，忽略 LLM 传入的 output_dir（安全原因）
        download_dir = str(self.output_dir)

        mcp = _get_kaggle_mcp_client()
        download_result = await mcp.call_tool(
            "download_competition_data",
            {"url": competition_ref, "output_dir": download_dir},
        )

        data_cards = []
        for file_path in download_result.get("files", []):
            data_cards.append({
                "file": file_path,
                "status": "downloaded",
            })

        return ToolResult(
            data={
                "competition_ref": competition_ref,
                "output_dir": download_dir,
                "files": data_cards,
                "status": "downloaded",
            }
        )
```

- [ ] **Step 4: 更新测试**

在 `test/unit/tools/test_kaggle_search.py` 中新增加 `work_root` 和落盘验证测试。由于 kaggle_search 当前是 MCP stub（`NotImplementedError`），测试需 mock `_get_kaggle_mcp_client`：

```python
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.kaggle_search import (
    KaggleCompetitionSearchTool,
    KaggleDatasetDownloadTool,
    KaggleDiscussionSearchTool,
)


@pytest.fixture
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def mock_mcp():
    """Mock Kaggle MCP 客户端 + parse_competition_info。"""
    mcp = MagicMock()
    mcp.call_tool = AsyncMock()
    with patch(
        "athena.tools.kaggle_search._get_kaggle_mcp_client", return_value=mcp
    ), patch(
        "athena.tools.kaggle_search.parse_competition_info", new_callable=AsyncMock
    ) as mock_parse:
        # 设置 parse_competition_info 返回一个简单的 metadata
        mock_meta = MagicMock()
        mock_meta.task_type = "binary_classification"
        mock_meta.data_type = "tabular"
        mock_meta.target_vars = ["target"]
        mock_meta.primary_metric.name = "accuracy"
        mock_meta.primary_metric.direction = "maximize"
        mock_meta.constraints = {}
        mock_parse.return_value = mock_meta
        yield mcp


@pytest.mark.asyncio
async def test_competition_search_writes_json(mock_mcp, tmp_work_root):
    mock_mcp.call_tool.return_value = "<html>competition page</html>"
    tool = KaggleCompetitionSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, competition_url="https://kaggle.com/c/test")
    assert result.success is True

    # 验证落盘
    info_file = Path(result.data["output_dir"]) / "competition_info.json"
    assert info_file.exists()
    on_disk = json.loads(info_file.read_text(encoding="utf-8"))
    assert on_disk["task_type"] == "binary_classification"


@pytest.mark.asyncio
async def test_discussion_search_writes_json(mock_mcp, tmp_work_root):
    mock_mcp.call_tool.return_value = json.dumps({
        "discussions": [
            {"title": "Top solution", "author": "user1",
             "summary": "Used XGBoost", "score": 0.95, "url": "https://..."}
        ]
    })
    tool = KaggleDiscussionSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, competition_url="https://kaggle.com/c/test")
    assert result.success is True

    # 验证落盘
    disc_file = Path(result.data["output_dir"]) / "discussions.json"
    assert disc_file.exists()
    on_disk = json.loads(disc_file.read_text(encoding="utf-8"))
    assert on_disk["discussion_count"] == 1


@pytest.mark.asyncio
async def test_dataset_download_writes_to_managed_dir(mock_mcp, tmp_work_root):
    mock_mcp.call_tool.return_value = {"files": ["/tmp/dataset/train.csv"]}
    tool = KaggleDatasetDownloadTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        competition_ref="test-comp",
        output_dir="/unsafe/llm/path",  # LLM 传入的路径应被忽略
    )
    assert result.success is True
    # 验证实际输出目录使用了构造器注入的路径，而非 LLM 传入的
    assert result.data["output_dir"] == str(
        Path(tmp_work_root) / "kaggle_dataset_download"
    )
```

- [ ] **Step 5: 运行 kaggle_search 测试**

```bash
cd c:/Users/YeBai/Athena && uv run pytest test/unit/tools/test_kaggle_search.py -v
```
Expected: 全部测试 PASS

- [ ] **Step 6: Commit**

```bash
git add src/athena/tools/kaggle_search.py test/unit/tools/test_kaggle_search.py
git commit -m "feat: kaggle_search tools write outputs to disk"
```

---

### Task 5: hf_dataset.py — 2 个工具落盘

**Files:**
- Modify: `src/athena/tools/hf_dataset.py`
- Modify: `test/unit/tools/test_hf_dataset.py`

**Interfaces:**
- Produces: `HFDatasetSearchTool(work_root)`, `HFDatasetDownloadTool(work_root)`

- [ ] **Step 1: 修改 `HFDatasetSearchTool` 加 `__init__` 和落盘**

在 [hf_dataset.py:26](src/athena/tools/hf_dataset.py#L26)：

```python
import json
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class HFDatasetSearchTool(BaseTool):
    """在 HuggingFace Hub 上搜索与比赛任务相关的数据集。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "hf_dataset_search"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        hf = _get_hf_api()
        keywords = input["task_keywords"]
        modality = input.get("modality", "")
        n_results = input.get("n_results", 10)

        queries: list[str] = []
        if modality:
            queries.append(f"{keywords} {modality}".strip())
        queries.append(keywords.strip())

        datasets: list[dict] = []
        tried_queries: list[str] = []

        for search_query in queries:
            tried_queries.append(search_query)
            try:
                results = list(hf.list_datasets(search=search_query, limit=n_results))
            except Exception as exc:
                return ToolResult(success=False, error=f"HF dataset search failed: {exc}")

            for ds in results:
                datasets.append({
                    "id": getattr(ds, "id", ""),
                    "description": getattr(ds, "description", "") or "",
                    "tags": getattr(ds, "tags", []) or [],
                    "downloads": getattr(ds, "downloads", 0) or 0,
                    "likes": getattr(ds, "likes", 0) or 0,
                })

            if datasets:
                break

        suggestion = ""
        if not datasets:
            suggestion = (
                f"No datasets found for {tried_queries}. "
                f"Consider broader keywords or a different task description."
            )

        data = {
            "datasets": datasets,
            "count": len(datasets),
            "search_query_used": tried_queries[-1],
            "fallback_chain": tried_queries,
            "suggestion": suggestion,
        }

        # 落盘 search_results.json
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "search_results.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ToolResult(data={**data, "output_dir": str(self.output_dir)})
```

- [ ] **Step 2: 修改 `HFDatasetDownloadTool` 加 `__init__` 和落盘**

在 [hf_dataset.py:112](src/athena/tools/hf_dataset.py#L112)：

```python
class HFDatasetDownloadTool(BaseTool):
    """从 HuggingFace Hub 下载数据集并保存到本地。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "hf_dataset_download"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ds_id = input["hf_dataset_id"]
        # 使用构造器注入的 output_dir，忽略 LLM 传入的路径（安全原因）
        download_dir = str(self.output_dir)

        Path(download_dir).mkdir(parents=True, exist_ok=True)

        try:
            local_path = snapshot_download(
                repo_id=ds_id, repo_type="dataset", local_dir=download_dir
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"HF dataset download failed for '{ds_id}': {exc}",
            )

        return ToolResult(
            data={
                "dataset_id": ds_id,
                "local_path": local_path,
                "status": "downloaded",
                "output_dir": download_dir,
            }
        )
```

- [ ] **Step 3: 更新测试**

在 `test/unit/tools/test_hf_dataset.py` 中：

```python
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.hf_dataset import HFDatasetDownloadTool, HFDatasetSearchTool


@pytest.fixture
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def mock_hf_api():
    """Mock HfApi 返回固定数据集列表。"""
    with patch("athena.tools.hf_dataset._get_hf_api") as mock_get:
        api = MagicMock()
        ds = MagicMock()
        ds.id = "user/test-dataset"
        ds.description = "A test dataset"
        ds.tags = ["tabular"]
        ds.downloads = 1000
        ds.likes = 50
        api.list_datasets.return_value = [ds]
        mock_get.return_value = api
        yield api


@pytest.mark.asyncio
async def test_dataset_search_writes_json(mock_hf_api, tmp_work_root):
    tool = HFDatasetSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, task_keywords="titanic survival", modality="tabular"
    )
    assert result.success is True
    assert result.data["count"] == 1

    # 验证落盘
    results_file = Path(result.data["output_dir"]) / "search_results.json"
    assert results_file.exists()
    on_disk = json.loads(results_file.read_text(encoding="utf-8"))
    assert on_disk["datasets"][0]["id"] == "user/test-dataset"


@pytest.mark.asyncio
async def test_dataset_download_uses_managed_dir(tmp_work_root):
    """下载工具应使用构造器注入的目录，而非 LLM 传入的路径。"""
    with patch("athena.tools.hf_dataset.snapshot_download") as mock_snap:
        mock_snap.return_value = str(Path(tmp_work_root) / "hf_dataset_download")
        tool = HFDatasetDownloadTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            hf_dataset_id="user/test-dataset",
            output_dir="/unsafe/llm/path",  # 应被忽略
        )
        assert result.success is True
        assert result.data["output_dir"] == str(
            Path(tmp_work_root) / "hf_dataset_download"
        )
        # 验证 snapshot_download 使用了安全的路径
        mock_snap.assert_called_once()
        call_kwargs = mock_snap.call_args.kwargs
        assert call_kwargs["local_dir"] == str(
            Path(tmp_work_root) / "hf_dataset_download"
        )
```

- [ ] **Step 4: 运行 hf_dataset 测试**

```bash
cd c:/Users/YeBai/Athena && uv run pytest test/unit/tools/test_hf_dataset.py -v
```
Expected: 全部测试 PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/hf_dataset.py test/unit/tools/test_hf_dataset.py
git commit -m "feat: hf_dataset tools write outputs to disk"
```

---

### Task 6: hf_model.py — 2 个工具落盘

**Files:**
- Modify: `src/athena/tools/hf_model.py`
- Modify: `test/unit/tools/test_hf_model.py`

**Interfaces:**
- Produces: `HFModelSearchTool(work_root)`, `HFModelDownloadTool(work_root)`

- [ ] **Step 1: 修改 `HFModelSearchTool` 加 `__init__` 和落盘**

在 [hf_model.py:25](src/athena/tools/hf_model.py#L25)：

```python
import json
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec


class HFModelSearchTool(BaseTool):
    """在 HuggingFace Hub 上搜索预训练模型。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "hf_model_search"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        hf = _get_hf_api()
        task_type = input["task_type"]
        architecture_hint = input.get("architecture_hint", "")
        n_results = input.get("n_results", 10)

        search_terms = task_type.replace("_", " ")
        if architecture_hint:
            search_terms = f"{architecture_hint} {search_terms}"

        try:
            results = list(hf.list_models(search=search_terms, limit=n_results))
        except Exception as exc:
            return ToolResult(success=False, error=f"HF model search failed: {exc}")

        models = []
        for m in results:
            models.append({
                "id": getattr(m, "modelId", getattr(m, "id", "")),
                "pipeline_tag": getattr(m, "pipeline_tag", "") or "",
                "tags": getattr(m, "tags", []) or [],
                "downloads": getattr(m, "downloads", 0) or 0,
                "likes": getattr(m, "likes", 0) or 0,
            })

        suggestion = ""
        if not models:
            suggestion = (
                f"No models found for '{search_terms}'. "
                f"Try a broader architecture hint or omit it."
            )

        data = {"models": models, "count": len(models), "suggestion": suggestion}

        # 落盘 search_results.json
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "search_results.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ToolResult(data={**data, "output_dir": str(self.output_dir)})
```

- [ ] **Step 2: 修改 `HFModelDownloadTool` 加 `__init__` 和落盘**

在 [hf_model.py:100](src/athena/tools/hf_model.py#L100)：

```python
class HFModelDownloadTool(BaseTool):
    """从 HuggingFace Hub 下载预训练模型权重到本地。"""

    spec = ToolSpec(...)  # 不变

    def __init__(self, work_root: str = "work") -> None:
        self.output_dir = Path(work_root) / "hf_model_download"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        model_id = input["hf_model_id"]
        # 使用构造器注入的 output_dir，忽略 LLM 传入的路径（安全原因）
        download_dir = str(self.output_dir)

        Path(download_dir).mkdir(parents=True, exist_ok=True)

        try:
            local_path = snapshot_download(repo_id=model_id, local_dir=download_dir)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"HF model download failed for '{model_id}': {exc}",
            )

        return ToolResult(
            data={
                "model_id": model_id,
                "model_path": local_path,
                "status": "downloaded",
                "output_dir": download_dir,
            }
        )
```

- [ ] **Step 3: 更新测试**

在 `test/unit/tools/test_hf_model.py` 中：

```python
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.hf_model import HFModelDownloadTool, HFModelSearchTool


@pytest.fixture
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def mock_hf_api():
    """Mock HfApi 返回固定模型列表。"""
    with patch("athena.tools.hf_model._get_hf_api") as mock_get:
        api = MagicMock()
        m = MagicMock()
        m.modelId = "google/vit-base"
        m.pipeline_tag = "image-classification"
        m.tags = ["pytorch"]
        m.downloads = 50000
        m.likes = 200
        api.list_models.return_value = [m]
        mock_get.return_value = api
        yield api


@pytest.mark.asyncio
async def test_model_search_writes_json(mock_hf_api, tmp_work_root):
    tool = HFModelSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_type="image_classification",
        modality="image",
        architecture_hint="vit",
    )
    assert result.success is True
    assert result.data["count"] == 1

    # 验证落盘
    results_file = Path(result.data["output_dir"]) / "search_results.json"
    assert results_file.exists()
    on_disk = json.loads(results_file.read_text(encoding="utf-8"))
    assert on_disk["models"][0]["id"] == "google/vit-base"


@pytest.mark.asyncio
async def test_model_download_uses_managed_dir(tmp_work_root):
    """下载工具应使用构造器注入的目录，而非 LLM 传入的路径。"""
    with patch("athena.tools.hf_model.snapshot_download") as mock_snap:
        mock_snap.return_value = str(Path(tmp_work_root) / "hf_model_download")
        tool = HFModelDownloadTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            hf_model_id="google/vit-base",
            output_dir="/unsafe/llm/path",  # 应被忽略
        )
        assert result.success is True
        assert result.data["output_dir"] == str(
            Path(tmp_work_root) / "hf_model_download"
        )
        mock_snap.assert_called_once()
        call_kwargs = mock_snap.call_args.kwargs
        assert call_kwargs["local_dir"] == str(
            Path(tmp_work_root) / "hf_model_download"
        )
```

- [ ] **Step 4: 运行 hf_model 测试**

```bash
cd c:/Users/YeBai/Athena && uv run pytest test/unit/tools/test_hf_model.py -v
```
Expected: 全部测试 PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/hf_model.py test/unit/tools/test_hf_model.py
git commit -m "feat: hf_model tools write outputs to disk"
```

---

### Task 7: 全量回归测试 + 最终验证

**Files:**
- (无代码改动，仅运行测试)

- [ ] **Step 1: 运行全部工具单元测试**

```bash
cd c:/Users/YeBai/Athena && uv run pytest test/unit/tools/ -v
```
Expected: 全部测试 PASS（包括旧测试的兼容性）

- [ ] **Step 2: 运行完整测试套件**

```bash
cd c:/Users/YeBai/Athena && uv run pytest -v
```
Expected: 全部测试 PASS，无回归

- [ ] **Step 3: 验证 Agent 构建不报错**

```bash
cd c:/Users/YeBai/Athena && uv run python -c "
from athena.agents.competition.task_understand_agent import build_task_understand_agent
agent = build_task_understand_agent(model='test', work_root='/tmp/test-work')
assert len(agent.config.tools) == 13
for tool in agent.config.tools.specs:
    print(f'  {tool.name}')
print('All 13 tools registered successfully')
"
```

- [ ] **Step 4: Commit (如有最终调整)**

```bash
git add -A
git commit -m "chore: final verification — all tests pass"
```

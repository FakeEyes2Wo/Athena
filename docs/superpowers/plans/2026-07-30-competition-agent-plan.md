# Competition Agent (TaskUnderstandAgent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build TaskUnderstandAgent — a single ReAct Agent that accepts a Kaggle competition prompt and autonomously completes the pipeline: research → dataset acquisition → data preparation → solution design → code generation → execution → submission.

**Architecture:** One BaseAgent subclass (TaskUnderstandAgent) holds a ToolRegistry with 10 tools across 5 files. Two existing stubs (task_parser.py, dataset_service.py) are filled as supporting services. The agent uses the existing Athena Agent loop, ContextManager, ArtifactStore, and SandboxRuntime infrastructure.

**Tech Stack:** Python ≥3.11, pydantic-ai ≥2.17.0, openai ≥2.48.0, huggingface_hub, Kaggle MCP server (external)

**Python environment:** `D:\Softwares\Miniconnda\envs\athena\python.exe`

## Global Constraints

- All prompts are in English; Pydantic `description` fields are in English
- All scoring/ranking/selection decisions must be bound to a versioned rubric with per-item evidence
- Occam's razor: use deterministic services before agents
- State objects hold only necessary fields; large objects are stored as artifact references
- Use existing `BaseAgent` + `ToolRegistry` + `AgentContext` infrastructure — no new framework code
- Kaggle interactions go through Kaggle MCP (Model Context Protocol server), accessed via tool layer
- HuggingFace interactions go through `huggingface_hub` Python API
- Concurrency-safe tools set `concurrency_safe=True`; tools that mutate filesystem or call external APIs set `concurrency_safe=False`
- `code_execute` uses existing `athena/execution/sandbox_runtime.py` for isolation
- Code comments at key positions (function docstrings, non-obvious logic, design decisions) are written in Chinese
- Do not touch `agents/search/`, `agents/control/`, `agents/policy/`

---

### Task 1: Add huggingface_hub dependency

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `huggingface_hub` available in project environment for Tasks 5 and 6

- [ ] **Step 1: Add dependency to pyproject.toml**

Add `"huggingface_hub>=0.20"` to the `dependencies` list in `pyproject.toml`:

```toml
dependencies = [
    "openai>=2.48.0",
    "pydantic>=2.0",
    "pydantic-ai>=2.17.0",
    "python-dotenv>=1.0",
    "pylatexenc>=2.10",
    "pymupdf>=1.24",
    "huggingface_hub>=0.20",
]
```

- [ ] **Step 2: Install the new dependency**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pip install "huggingface_hub>=0.20"
```

Expected: Clean install, no errors.

- [ ] **Step 3: Verify import**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -c "from huggingface_hub import HfApi, hf_hub_download, snapshot_download; print('OK')"
```

Expected: Prints `OK` without error.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add huggingface_hub dependency for HF dataset/model tools"
```

---

### Task 2: Fill workflows/prepare/task_parser.py

**Files:**
- Modify: `src/athena/workflows/prepare/task_parser.py`
- Create: `test/unit/workflows/test_task_parser.py`

**Interfaces:**
- Consumes: `athena.core.schemas.TaskMetaData`, `athena.core.schemas.MetricSpec`, `athena.utils.single_turn_chat.single_turn_chat`
- Produces: `parse_competition_info(raw_text: str, source_url: str) -> TaskMetaData` — LLM 解析抓取的比赛文本，产出结构化元数据

- [ ] **Step 1: Create test directory and write failing test**

```bash
mkdir -p test/unit/workflows
```

```python
# test/unit/workflows/test_task_parser.py
import pytest
from athena.workflows.prepare.task_parser import parse_competition_info

MOCK_KAGGLE_TEXT = """
# Titanic - Machine Learning from Disaster
Predict survival on the Titanic using passenger data.

## Evaluation
Submissions are evaluated on accuracy.

## Data
- train.csv: 891 rows, 12 columns including Survived (target)
- test.csv: 418 rows, 11 columns (no Survived)
"""


def test_parse_basic_competition_info():
    result = parse_competition_info(
        MOCK_KAGGLE_TEXT, "https://kaggle.com/c/titanic"
    )
    assert result.task_type == "binary_classification"
    assert result.data_type == "tabular"
    assert result.target_vars == ["Survived"]
    assert result.primary_metric.name == "accuracy"
    assert result.primary_metric.direction == "maximize"


def test_parse_handles_unknown_fields():
    result = parse_competition_info(
        "Some competition with no details.", "https://example.com"
    )
    assert isinstance(result.task_type, str) and len(result.task_type) > 0
    assert isinstance(result.primary_metric.name, str)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/workflows/test_task_parser.py -v
```

Expected: FAIL — `ImportError` or `AttributeError`（函数尚未实现）。

- [ ] **Step 3: Implement parse_competition_info**

用以下内容完整替换 `src/athena/workflows/prepare/task_parser.py`：

```python
"""Parse raw competition description text into structured TaskMetaData.

Uses an LLM call to extract task type, data modality, target variables,
primary metric, and constraints from unstructured competition text.
"""

import json

from athena.core.schemas import MetricSpec, TaskMetaData
from athena.utils.single_turn_chat import single_turn_chat

# 系统提示词：让 LLM 从原始比赛描述中提取结构化字段
_PARSE_SYSTEM_PROMPT = """\
You are a competition metadata parser. Given a raw competition description,
extract structured metadata as JSON.

Output a JSON object with these fields:
- task_type: one of "binary_classification", "multi_class_classification",
  "regression", "clustering", "time_series_forecasting", "object_detection",
  "image_segmentation", "image_generation", "style_transfer", "nlp",
  "recommendation", "other"
- data_type: one of "tabular", "text", "image", "time_series", "video",
  "audio", "multi_modal", "other"
- target_vars: list of target variable / label column names (strings)
- primary_metric_name: the main evaluation metric name (e.g. "accuracy",
  "f1", "rmse", "log_loss")
- primary_metric_direction: "maximize" or "minimize"
- constraints: list of constraint strings

If a field cannot be determined, use: task_type="other", data_type="other",
primary_metric_name="unknown", primary_metric_direction="maximize".
"""


def parse_competition_info(raw_text: str, source_url: str) -> TaskMetaData:
    """通过 LLM 将比赛描述文本解析为 TaskMetaData。

    Args:
        raw_text: 抓取的比赛描述或 markdown 内容。
        source_url: 比赛 URL，用于溯源。

    Returns:
        包含 task_type, data_type, target_vars, primary_metric, constraints 的 TaskMetaData。
    """
    user_prompt = (
        f"Competition URL: {source_url}\n\n"
        f"Competition description:\n{raw_text}"
    )
    result = single_turn_chat(
        system_prompt=_PARSE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_format={"type": "json_object"},
    )
    data = json.loads(result)

    return TaskMetaData(
        task_type=data.get("task_type", "other"),
        data_type=data.get("data_type", "other"),
        target_vars=data.get("target_vars", []),
        primary_metric=MetricSpec(
            name=data.get("primary_metric_name", "unknown"),
            direction=data.get("primary_metric_direction", "maximize"),
        ),
        constraints=data.get("constraints", []),
    )
```

- [ ] **Step 4: Verify single_turn_chat exists**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -c "from athena.utils.single_turn_chat import single_turn_chat; print('OK')"
```

Expected: `OK`。

- [ ] **Step 5: Run test to verify it passes**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/workflows/test_task_parser.py -v
```

Expected: 2 passing tests。

- [ ] **Step 6: Commit**

```bash
git add src/athena/workflows/prepare/task_parser.py test/unit/workflows/test_task_parser.py
git commit -m "feat: implement parse_competition_info in task_parser"
```

---

### Task 3: Fill workflows/prepare/dataset_service.py

**Files:**
- Modify: `src/athena/workflows/prepare/dataset_service.py`
- Create: `test/unit/workflows/test_dataset_service.py`

**Interfaces:**
- Consumes: `athena.storage.artifact_store.ArtifactStore` (Protocol), `athena.core.schemas.DataCard`, `pandas`
- Produces: `async def create_data_card(dataset_path: str, store: ArtifactStore) -> DataCard` — 保存不可变原始副本，计算 SHA-256 指纹，产出包含 schema 引用的 DataCard

- [ ] **Step 1: Write the failing test**

```python
# test/unit/workflows/test_dataset_service.py
import json
import tempfile
from pathlib import Path

import pytest
from athena.storage.artifact_store import LocalArtifactStore
from athena.workflows.prepare.dataset_service import create_data_card


@pytest.fixture
def temp_store():
    with tempfile.TemporaryDirectory() as tmp:
        yield LocalArtifactStore(Path(tmp) / "artifacts")


@pytest.fixture
def sample_csv():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "train.csv"
        csv_path.write_text(
            "PassengerId,Survived,Pclass,Name\n"
            "1,0,3,Smith\n2,1,1,Jones\n3,1,3,Brown\n"
        )
        yield csv_path


@pytest.mark.asyncio
async def test_create_data_card_fingerprint(temp_store, sample_csv):
    card = await create_data_card(str(sample_csv), temp_store)
    assert card.dataset_ref.startswith("sha256:")
    assert len(card.fingerprint) == 64
    assert card.schema_ref.startswith("sha256:")
    assert card.split_manifest_ref is None


@pytest.mark.asyncio
async def test_create_data_card_idempotent(temp_store, sample_csv):
    card1 = await create_data_card(str(sample_csv), temp_store)
    card2 = await create_data_card(str(sample_csv), temp_store)
    assert card1.dataset_ref == card2.dataset_ref
    assert card1.fingerprint == card2.fingerprint


@pytest.mark.asyncio
async def test_create_data_card_schema(temp_store, sample_csv):
    card = await create_data_card(str(sample_csv), temp_store)
    schema_text = await temp_store.get_text(card.schema_ref)
    schema = json.loads(schema_text)
    assert "PassengerId" in schema["columns"]
    assert "Survived" in schema["columns"]
    assert schema["row_count"] == 3
    assert schema["column_count"] == 4
```

- [ ] **Step 2: Run test to verify it fails**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/workflows/test_dataset_service.py -v
```

Expected: FAIL — `ImportError` or `AttributeError`。

- [ ] **Step 3: Implement create_data_card**

用以下内容完整替换 `src/athena/workflows/prepare/dataset_service.py`：

```python
"""Dataset ingestion service: immutable copy, fingerprint, DataCard.

Saves an immutable raw-data copy before any cleaning, sampling, or feature
engineering. Computes a SHA-256 content fingerprint and produces a schema
artifact referenced from the returned DataCard.
"""

import hashlib
import json
from pathlib import Path

import pandas as pd

from athena.core.schemas import DataCard
from athena.storage.artifact_store import ArtifactStore


def _infer_schema(df: pd.DataFrame) -> dict:
    """从 DataFrame 构建轻量级列级 schema 摘要。"""
    columns = {}
    for col_name in df.columns:
        col = df[col_name]
        columns[str(col_name)] = {
            "dtype": str(col.dtype),
            "null_count": int(col.isnull().sum()),
            "unique_count": int(col.nunique()),
        }
    return {
        "columns": columns,
        "row_count": len(df),
        "column_count": len(df.columns),
    }


async def create_data_card(dataset_path: str, store: ArtifactStore) -> DataCard:
    """接入数据集：保存不可变原始副本，计算指纹，产出 schema artifact。

    在清洗、抽样或特征工程之前保存原始数据。计算文件字节的 SHA-256 指纹，
    将原始内容存入 ArtifactStore，并写入 schema 摘要 artifact。

    Args:
        dataset_path: CSV 或 Parquet 文件在磁盘上的路径。
        store: 内容寻址持久化的 ArtifactStore。

    Returns:
        包含 dataset_ref（不可变原始副本）、fingerprint 和 schema_ref 的 DataCard。
    """
    raw_path = Path(dataset_path)
    if not raw_path.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")

    raw_bytes = raw_path.read_bytes()
    fingerprint = hashlib.sha256(raw_bytes).hexdigest()

    # 保存不可变原始副本到 artifact store
    dataset_ref = await store.put_bytes(raw_bytes)

    # 读取数据用于 schema 推断
    if raw_path.suffix.lower() == ".parquet":
        df = pd.read_parquet(raw_path)
    else:
        df = pd.read_csv(raw_path)

    schema = _infer_schema(df)
    schema_json = json.dumps(schema, ensure_ascii=False)
    schema_ref = await store.put_text(schema_json)

    return DataCard(
        dataset_ref=dataset_ref,
        fingerprint=fingerprint,
        schema_ref=schema_ref,
        split_manifest_ref=None,  # 原始数据无 split manifest
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/workflows/test_dataset_service.py -v
```

Expected: 3 passing tests。

- [ ] **Step 5: Commit**

```bash
git add src/athena/workflows/prepare/dataset_service.py test/unit/workflows/test_dataset_service.py
git commit -m "feat: implement create_data_card in dataset_service"
```

---
### Task 4: Build tools/kaggle_search.py — Kaggle MCP wrapper

**Files:**
- Create: `src/athena/tools/__init__.py` (empty)
- Create: `src/athena/tools/kaggle_search.py`
- Create: `test/unit/tools/test_kaggle_search.py`

**Interfaces:**
- Consumes: Kaggle MCP server (external), `athena.workflows.prepare.task_parser.parse_competition_info`
- Produces: Two BaseTool subclasses

| Tool class | Tool name | Input | Output |
|---|---|---|---|
| `KaggleCompetitionSearchTool` | `kaggle_competition_search` | `competition_url: str` | `TaskMetaData` JSON + full competition description as artifact ref |
| `KaggleDiscussionSearchTool` | `kaggle_discussion_search` | `competition_url: str, top_k: int = 10` | Discussion summary list as artifact ref |

- [ ] **Step 1: Create directories and write failing test**

```bash
mkdir -p src/athena/tools test/unit/tools
```

```python
# test/unit/tools/test_kaggle_search.py
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.kaggle_search import (
    KaggleCompetitionSearchTool,
    KaggleDiscussionSearchTool,
)

# 模拟 Kaggle MCP 返回的比赛页面内容
MOCK_COMPETITION_HTML = """
<h1>Titanic - Machine Learning from Disaster</h1>
<p>Predict survival on the Titanic</p>
<h2>Evaluation</h2><p>Accuracy</p>
"""

MOCK_DISCUSSION_JSON = {
    "discussions": [
        {"title": "Top 3% solution", "author": "user1",
         "summary": "Used XGBoost with feature engineering", "score": 0.82,
         "url": "https://kaggle.com/c/titanic/discussion/1"}
    ]
}


@pytest.fixture
def mock_mcp_client():
    """模拟 Kaggle MCP 客户端。"""
    with patch("athena.tools.kaggle_search._get_kaggle_mcp_client") as mock:
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value=MOCK_COMPETITION_HTML)
        mock.return_value = client
        yield mock


@pytest.mark.asyncio
async def test_competition_search_returns_task_metadata(mock_mcp_client):
    tool = KaggleCompetitionSearchTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, competition_url="https://kaggle.com/c/titanic"
    )
    assert result.success is True
    assert "task_type" in str(result.data)


@pytest.mark.asyncio
async def test_discussion_search_returns_summaries(mock_mcp_client):
    tool = KaggleDiscussionSearchTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, competition_url="https://kaggle.com/c/titanic", top_k=5
    )
    assert result.success is True
    assert "discussions" in str(result.data).lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_kaggle_search.py -v
```

Expected: FAIL — `ImportError`。

- [ ] **Step 3: Implement Kaggle MCP tools**

Create `src/athena/tools/__init__.py` (empty file).

Create `src/athena/tools/kaggle_search.py`:

```python
"""Kaggle MCP wrapper tools — 比赛信息搜索和 discussion 搜索。

通过 Kaggle MCP (Model Context Protocol) 服务与 Kaggle 平台交互。
该 MCP 服务负责页面抓取、鉴权和 API 调用，本模块只做薄封装。
"""

import json
from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.workflows.prepare.task_parser import parse_competition_info


# ── Kaggle MCP 客户端获取 ──

def _get_kaggle_mcp_client() -> Any:
    """获取 Kaggle MCP 客户端。

    当前实现为 stub：TODO 后续接入实际 MCP 服务。
    """
    # TODO: 与实际 Kaggle MCP 服务建立连接
    raise NotImplementedError("Kaggle MCP client not yet configured")


# ── 比赛信息搜索工具 ──

class KaggleCompetitionSearchTool(BaseTool):
    """搜索 Kaggle 比赛基本信息：描述、评价指标、数据格式、提交要求。"""

    spec = ToolSpec(
        name="kaggle_competition_search",
        description=(
            "Search Kaggle competition for description, evaluation metric, "
            "data format, and submission requirements. Returns structured "
            "TaskMetaData and full description."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "competition_url": {
                    "type": "string",
                    "description": "Kaggle competition URL or slug.",
                }
            },
            "required": ["competition_url"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 外部 HTTP 调用，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_url = input["competition_url"]

        # 通过 Kaggle MCP 拉取比赛页面内容
        mcp = _get_kaggle_mcp_client()
        raw_html = await mcp.call_tool(
            "fetch_competition_page", {"url": competition_url}
        )

        # 用 LLM 解析为结构化 TaskMetaData
        metadata = parse_competition_info(raw_html, competition_url)

        return ToolResult(
            data={
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
        )


# ── Discussion 搜索工具 ──

class KaggleDiscussionSearchTool(BaseTool):
    """搜索 Kaggle Discussion/Notebook 中的 Top 方案思路。"""

    spec = ToolSpec(
        name="kaggle_discussion_search",
        description=(
            "Search Kaggle competition discussions and notebooks for top "
            "solutions, feature engineering approaches, and model ideas. "
            "Returns a list of summarized entries with scores and links."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "competition_url": {
                    "type": "string",
                    "description": "Kaggle competition URL or slug.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of top discussions to return.",
                    "default": 10,
                },
            },
            "required": ["competition_url"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_url = input["competition_url"]
        top_k = input.get("top_k", 10)

        mcp = _get_kaggle_mcp_client()
        raw = await mcp.call_tool(
            "search_discussions",
            {"url": competition_url, "top_k": top_k},
        )
        discussions = json.loads(raw) if isinstance(raw, str) else raw

        # 构建结构化摘要列表
        summaries = []
        for d in discussions.get("discussions", []):
            summaries.append({
                "title": d.get("title", ""),
                "author": d.get("author", ""),
                "approach_summary": d.get("summary", ""),
                "score": d.get("score"),
                "url": d.get("url", ""),
            })

        return ToolResult(
            data={
                "discussion_count": len(summaries),
                "discussions": summaries,
            }
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_kaggle_search.py -v
```

Expected: 2 passing tests。

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/__init__.py src/athena/tools/kaggle_search.py test/unit/tools/test_kaggle_search.py
git commit -m "feat: add Kaggle MCP wrapper tools (competition + discussion search)"
```

---

**Additional tool for Task 4 — add to `kaggle_search.py`:**

| Tool class | Tool name | Input | Output |
|---|---|---|---|
| `KaggleDatasetDownloadTool` | `kaggle_dataset_download` | `competition_ref: str, output_dir: str` | `DataCard` (dataset_ref, fingerprint, schema_ref) |

Add this class to `src/athena/tools/kaggle_search.py` alongside the other two:

```python
class KaggleDatasetDownloadTool(BaseTool):
    """通过 Kaggle MCP 下载比赛数据集并生成 DataCard。"""

    spec = ToolSpec(
        name="kaggle_dataset_download",
        description=(
            "Download the competition dataset via Kaggle MCP. Saves data "
            "to the specified output directory and returns a DataCard with "
            "content fingerprint and schema information."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "competition_ref": {
                    "type": "string",
                    "description": "Kaggle competition URL or slug.",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Local directory path to save the dataset.",
                },
            },
            "required": ["competition_ref", "output_dir"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 下载写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_ref = input["competition_ref"]
        output_dir = input["output_dir"]

        # 通过 Kaggle MCP 下载数据集
        mcp = _get_kaggle_mcp_client()
        download_result = await mcp.call_tool(
            "download_competition_data",
            {"url": competition_ref, "output_dir": output_dir},
        )

        # 对每个下载的数据文件生成 DataCard
        data_cards = []
        for file_path in download_result.get("files", []):
            from athena.workflows.prepare.dataset_service import create_data_card
            # 注意：此处需要 ArtifactStore 实例，由调用上下文注入
            # 作为初始实现，返回文件路径让 Agent 后续处理
            data_cards.append({
                "file": file_path,
                "status": "downloaded",
            })

        return ToolResult(
            data={
                "competition_ref": competition_ref,
                "output_dir": output_dir,
                "files": data_cards,
                "status": "downloaded",
            }
        )
```

Add the corresponding test:

```python
# In test/unit/tools/test_kaggle_search.py — add:
from athena.tools.kaggle_search import KaggleDatasetDownloadTool


@pytest.mark.asyncio
async def test_dataset_download_returns_file_list(mock_mcp_client):
    tool = KaggleDatasetDownloadTool()
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        competition_ref="https://kaggle.com/c/titanic",
        output_dir="/tmp/titanic_data",
    )
    assert result.success is True
    assert "files" in result.data
    assert result.data["status"] == "downloaded"
```

Also update `build_task_understand_agent` in Task 9 to register the new tool:

```python
# Add to imports:
from athena.tools.kaggle_search import KaggleDatasetDownloadTool  # alongside existing imports

# Add to tool registration:
tools.register(KaggleDatasetDownloadTool())
```

Update the total tool count in Task 11 from 12 to **13**.

### Task 5: Build tools/hf_dataset.py — HuggingFace dataset tools

**Files:**
- Create: `src/athena/tools/hf_dataset.py`
- Create: `test/unit/tools/test_hf_dataset.py`

**Interfaces:**
- Consumes: `huggingface_hub.HfApi`, `athena.workflows.prepare.dataset_service.create_data_card`
- Produces: Two BaseTool subclasses

| Tool class | Tool name | Input | Output |
|---|---|---|---|
| `HFDatasetSearchTool` | `hf_dataset_search` | `task_keywords: str, modality: str, n_results: int` | Matching dataset list |
| `HFDatasetDownloadTool` | `hf_dataset_download` | `hf_dataset_id: str, output_dir: str` | Download confirmation with local path |

- [ ] **Step 1: Write the failing test**

```python
# test/unit/tools/test_hf_dataset.py
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.hf_dataset import HFDatasetSearchTool


# 模拟 HF API 返回的数据集搜索结果
MOCK_HF_RESULTS = [
    type("DsInfo", (), {
        "id": "user/titanic-similar",
        "description": "Similar passenger data",
        "tags": ["tabular", "classification"],
        "downloads": 500,
        "likes": 20,
        "lastModified": "2025-01-01",
    })()
]


@pytest.fixture
def mock_hf_api():
    with patch("athena.tools.hf_dataset.HfApi") as mock:
        api = mock.return_value
        api.list_datasets.return_value = MOCK_HF_RESULTS
        yield mock


@pytest.mark.asyncio
async def test_dataset_search_returns_list(mock_hf_api):
    tool = HFDatasetSearchTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_keywords="titanic passenger survival classification",
        modality="tabular",
        n_results=5,
    )
    assert result.success is True
    assert len(result.data["datasets"]) == 1
    assert result.data["datasets"][0]["id"] == "user/titanic-similar"


@pytest.mark.asyncio
async def test_dataset_search_empty_results(mock_hf_api):
    mock_hf_api.return_value.list_datasets.return_value = []
    tool = HFDatasetSearchTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, task_keywords="nonexistent", modality="tabular", n_results=5
    )
    assert result.success is True
    assert len(result.data["datasets"]) == 0
    assert "suggestion" in result.data
```

- [ ] **Step 2: Run test to verify it fails**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_hf_dataset.py -v
```

Expected: FAIL — `ImportError`。

- [ ] **Step 3: Implement HF dataset tools**

```python
"""HuggingFace dataset search and download tools.

Uses huggingface_hub Python API to search for similar datasets on
HuggingFace Hub and download them locally. Downloaded datasets are
ingested via dataset_service.create_data_card to produce DataCard.
"""

from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

# HF API 客户端（延迟初始化，与 hf_model 共享）
_hf_api: HfApi | None = None


def _get_hf_api() -> HfApi:
    global _hf_api
    if _hf_api is None:
        _hf_api = HfApi()
    return _hf_api


class HFDatasetSearchTool(BaseTool):
    """在 HuggingFace Hub 上搜索与比赛任务相关的数据集。"""

    spec = ToolSpec(
        name="hf_dataset_search",
        description=(
            "Search HuggingFace Hub for datasets related to the competition "
            "task. Use keywords, modality filter, and result count to find "
            "datasets that can augment the competition data."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_keywords": {
                    "type": "string",
                    "description": "Space-separated keywords describing the task.",
                },
                "modality": {
                    "type": "string",
                    "description": "Data modality: tabular, text, image, etc.",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": 10,
                },
            },
            "required": ["task_keywords", "modality"],
            "additionalProperties": False,
        },
        concurrency_safe=True,  # 只读搜索，可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        hf = _get_hf_api()
        keywords = input["task_keywords"]
        n_results = input.get("n_results", 10)

        try:
            results = list(hf.list_datasets(search=keywords, limit=n_results))
        except Exception as exc:
            return ToolResult(success=False, error=f"HF dataset search failed: {exc}")

        datasets = []
        for ds in results:
            datasets.append({
                "id": getattr(ds, "id", ""),
                "description": getattr(ds, "description", "") or "",
                "tags": getattr(ds, "tags", []) or [],
                "downloads": getattr(ds, "downloads", 0) or 0,
                "likes": getattr(ds, "likes", 0) or 0,
            })

        # 空结果时给 LLM 搜索建议
        suggestion = ""
        if not datasets:
            suggestion = (
                f"No datasets found for '{keywords}'. "
                f"Consider broadening keywords or removing modality filter."
            )

        return ToolResult(
            data={"datasets": datasets, "count": len(datasets), "suggestion": suggestion}
        )


class HFDatasetDownloadTool(BaseTool):
    """从 HuggingFace Hub 下载数据集并保存到本地。"""

    spec = ToolSpec(
        name="hf_dataset_download",
        description=(
            "Download a dataset from HuggingFace Hub by its dataset ID. "
            "Saves data to the specified output directory."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hf_dataset_id": {
                    "type": "string",
                    "description": "HuggingFace dataset ID (e.g. 'user/dataset-name').",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Local directory path to save the dataset.",
                },
            },
            "required": ["hf_dataset_id", "output_dir"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ds_id = input["hf_dataset_id"]
        output_dir = input["output_dir"]

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        try:
            local_path = snapshot_download(
                repo_id=ds_id, repo_type="dataset", local_dir=output_dir
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
            }
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_hf_dataset.py -v
```

Expected: 2 passing tests。

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/hf_dataset.py test/unit/tools/test_hf_dataset.py
git commit -m "feat: add HF dataset search and download tools"
```

---
### Task 6: Build tools/hf_model.py — HuggingFace model tools

**Files:**
- Create: `src/athena/tools/hf_model.py`
- Create: `test/unit/tools/test_hf_model.py`

**Interfaces:**
- Consumes: `huggingface_hub.HfApi`, `huggingface_hub.snapshot_download`
- Produces: Two BaseTool subclasses

| Tool class | Tool name | Input | Output |
|---|---|---|---|
| `HFModelSearchTool` | `hf_model_search` | `task_type: str, modality: str, architecture_hint: str, n_results: int` | Matching model list |
| `HFModelDownloadTool` | `hf_model_download` | `hf_model_id: str, output_dir: str` | `model_path: str` |

- [ ] **Step 1: Write the failing test**

```python
# test/unit/tools/test_hf_model.py
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.hf_model import HFModelSearchTool


MOCK_MODEL_RESULTS = [
    type("ModelInfo", (), {
        "modelId": "google/vit-base-patch16-224",
        "pipeline_tag": "image-classification",
        "tags": ["pytorch", "vision"],
        "downloads": 100000,
        "likes": 500,
        "lastModified": "2025-06-01",
    })()
]


@pytest.fixture
def mock_hf_api():
    with patch("athena.tools.hf_model.HfApi") as mock:
        api = mock.return_value
        api.list_models.return_value = MOCK_MODEL_RESULTS
        yield mock


@pytest.mark.asyncio
async def test_model_search_returns_list(mock_hf_api):
    tool = HFModelSearchTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_type="image_classification",
        modality="image",
        architecture_hint="vit transformer",
        n_results=5,
    )
    assert result.success is True
    assert len(result.data["models"]) == 1
    assert result.data["models"][0]["id"] == "google/vit-base-patch16-224"


@pytest.mark.asyncio
async def test_model_search_empty_is_ok(mock_hf_api):
    mock_hf_api.return_value.list_models.return_value = []
    tool = HFModelSearchTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, task_type="nonexistent", modality="tabular",
        architecture_hint="", n_results=5
    )
    assert result.success is True
    assert len(result.data["models"]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_hf_model.py -v
```

Expected: FAIL — `ImportError`。

- [ ] **Step 3: Implement HF model tools**

```python
"""HuggingFace model search and download tools.

Uses huggingface_hub Python API to search for pre-trained models
on HuggingFace Hub and download their weights locally.
"""

from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

# HF API 客户端（延迟初始化，与 hf_dataset 共享同一全局变量）
_hf_api: HfApi | None = None


def _get_hf_api() -> HfApi:
    global _hf_api
    if _hf_api is None:
        _hf_api = HfApi()
    return _hf_api


class HFModelSearchTool(BaseTool):
    """在 HuggingFace Hub 上搜索预训练模型。"""

    spec = ToolSpec(
        name="hf_model_search",
        description=(
            "Search HuggingFace Hub for pre-trained models matching "
            "the competition task type and modality."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "Task type: image_classification, text_classification, etc.",
                },
                "modality": {
                    "type": "string",
                    "description": "Data modality: tabular, text, image, etc.",
                },
                "architecture_hint": {
                    "type": "string",
                    "description": "Optional architecture keyword (e.g. 'vit', 'bert', 'resnet').",
                    "default": "",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Maximum number of results.",
                    "default": 10,
                },
            },
            "required": ["task_type", "modality"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        hf = _get_hf_api()
        task_type = input["task_type"]
        architecture_hint = input.get("architecture_hint", "")
        n_results = input.get("n_results", 10)

        # 搜索词：架构提示 + 任务类型
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

        return ToolResult(
            data={"models": models, "count": len(models), "suggestion": suggestion}
        )


class HFModelDownloadTool(BaseTool):
    """从 HuggingFace Hub 下载预训练模型权重到本地。"""

    spec = ToolSpec(
        name="hf_model_download",
        description=(
            "Download pre-trained model weights from HuggingFace Hub. "
            "Returns the local path to the downloaded model files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hf_model_id": {
                    "type": "string",
                    "description": "HuggingFace model ID (e.g. 'google/vit-base-patch16-224').",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Local directory to save model weights.",
                },
            },
            "required": ["hf_model_id", "output_dir"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        model_id = input["hf_model_id"]
        output_dir = input["output_dir"]

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        try:
            local_path = snapshot_download(repo_id=model_id, local_dir=output_dir)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"HF model download failed for '{model_id}': {exc}",
            )

        return ToolResult(
            data={"model_id": model_id, "model_path": local_path, "status": "downloaded"}
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_hf_model.py -v
```

Expected: 2 passing tests。

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/hf_model.py test/unit/tools/test_hf_model.py
git commit -m "feat: add HF model search and download tools"
```

---
### Task 7: Build tools/data_prepare.py — EDA and data cleaning tools

**Files:**
- Create: `src/athena/tools/data_prepare.py`
- Create: `test/unit/tools/test_data_prepare.py`

**Interfaces:**
- Consumes: `athena.core.schemas.DataCard`, LLM via `single_turn_chat`, `pandas`, `athena.execution.sandbox_runtime`
- Produces: Two BaseTool subclasses

| Tool class | Tool name | Input | Output |
|---|---|---|---|
| `DataAnalyzeTool` | `data_analyze` | `data_card_refs: list[str]` (artifact refs to DataCard JSON) | EDA report artifact ref |
| `DataCleanCodeGenTool` | `data_clean_code_gen` | `eda_report_ref: str, data_card_refs: list[str]` | Cleaning script artifact ref + cleaned data DataCard |

- [ ] **Step 1: Write the failing test**

```python
# test/unit/tools/test_data_prepare.py
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.storage.artifact_store import LocalArtifactStore
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool


@pytest.fixture
def mock_llm():
    """Mock LLM 返回固定的 EDA 报告。"""
    with patch(
        "athena.tools.data_prepare.single_turn_chat"
    ) as mock:
        mock.return_value = json.dumps({
            "distributions": {"PassengerId": "uniform", "Survived": "binary"},
            "missing_values": {"Age": 177},
            "outliers": {"Fare": "right-skewed with extreme values"},
            "correlations": {"Pclass-Survived": -0.34},
            "class_balance": {"Survived": {"0": 549, "1": 342}},
        })
        yield mock


@pytest.mark.asyncio
async def test_data_analyze_returns_eda_report(mock_llm):
    tool = DataAnalyzeTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        data_card_refs=["sha256:aaaa"],
    )
    assert result.success is True
    assert "eda_report_ref" in result.data


@pytest.mark.asyncio
async def test_data_analyze_empty_refs_errors():
    tool = DataAnalyzeTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, data_card_refs=[])
    assert result.success is False
```

```python
import json  # needed at top of test file
```

- [ ] **Step 2: Run test to verify it fails**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_data_prepare.py -v
```

Expected: FAIL — `ImportError`。

- [ ] **Step 3: Implement data prepare tools**

```python
"""Data preparation tools — EDA 分析和数据清洗代码生成。

data_analyze: 对数据集进行探索性数据分析，生成 EDA 报告。
data_clean_code_gen: 基于 EDA 报告生成清洗脚本，执行后产出清洗后数据。
"""

import json

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat

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

# 清洗代码生成提示词
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
    """对数据集进行探索性数据分析（EDA）。"""

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
            return ToolResult(success=False, error="At least one DataCard ref is required")

        # 从 DataCard 中收集 schema 信息，构建分析 prompt
        # 实际实现需要从 ArtifactStore 读取 schema
        schema_summaries = []
        for ref in refs:
            schema_summaries.append(f"Dataset ref: {ref}")

        user_prompt = (
            "Analyze the following datasets and produce an EDA report:\n\n"
            + "\n".join(schema_summaries)
        )

        eda_result = single_turn_chat(
            system_prompt=_EDA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        # EDA 报告作为 JSON artifact 返回
        return ToolResult(
            data={
                "eda_report": json.loads(eda_result),
                "data_card_refs": refs,
            }
        )


class DataCleanCodeGenTool(BaseTool):
    """基于 EDA 报告生成数据清洗代码并执行。"""

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
        # 基于 EDA 报告用 LLM 生成清洗脚本
        eda_ref = input["eda_report_ref"]
        data_refs = input["data_card_refs"]

        user_prompt = (
            f"EDA report reference: {eda_ref}\n"
            f"Dataset references: {', '.join(data_refs)}\n\n"
            f"Generate a Python cleaning script for these datasets."
        )

        clean_script = single_turn_chat(
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_data_prepare.py -v
```

Expected: 2 passing tests。

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/data_prepare.py test/unit/tools/test_data_prepare.py
git commit -m "feat: add data analysis and cleaning code generation tools"
```

---
### Task 8: Build tools/baseline_builder.py — Solution design, code generation, execution, submission

**Files:**
- Create: `src/athena/tools/baseline_builder.py`
- Create: `test/unit/tools/test_baseline_builder.py`

**Interfaces:**
- Consumes: LLM via `single_turn_chat`, `athena.execution.sandbox_runtime`, `athena.core.schemas.TaskMetaData`, `athena.core.schemas.DataCard`
- Produces: Four BaseTool subclasses

| Tool class | Tool name | Input | Output |
|---|---|---|---|
| `SolutionDesignTool` | `solution_design` | `task_metadata: dict, eda_report_ref: str, research_refs: list[str], model_candidates: list[dict]` | Solution plan artifact ref (JSON) |
| `ProjectCodeGenTool` | `project_code_gen` | `solution_plan_ref: str, data_card_refs: list[str], model_path: str, submission_format: str` | Code artifact ref (tar/zip of project files) |
| `CodeExecuteTool` | `code_execute` | `code_artifact_ref: str, entry_point: str ("train" / "infer")` | Execution log + metrics/checkpoint/predictions artifact refs |
| `SubmissionBuildTool` | `submission_build` | `predictions_path: str, submission_format: str` | `submission.csv` artifact ref |

- [ ] **Step 1: Write the failing test**

```python
# test/unit/tools/test_baseline_builder.py
import json
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.baseline_builder import (
    SolutionDesignTool,
    ProjectCodeGenTool,
    SubmissionBuildTool,
)


@pytest.fixture
def mock_llm():
    """Mock LLM 返回固定的方案设计。"""
    with patch("athena.tools.baseline_builder.single_turn_chat") as mock:
        mock.return_value = json.dumps({
            "model_selection": "XGBoost with default hyperparameters",
            "pipeline_structure": "Feature engineering → Train → Predict",
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
    # Rubric 必须存在（设计要求）
    assert "rubric" in plan


@pytest.mark.asyncio
async def test_solution_design_without_rubric_rejected(mock_llm):
    """如果 LLM 返回的方案缺少 rubric，工具应报告失败。"""
    mock_llm.return_value = json.dumps({
        "model_selection": "some model",
        "pipeline_structure": "some pipeline",
        "training_strategy": "some strategy",
        "loss_metric": "some metric",
        # 故意缺少 rubric
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_baseline_builder.py -v
```

Expected: FAIL — `ImportError`。

- [ ] **Step 3: Implement baseline builder tools**

```python
"""Baseline builder tools — 方案设计、代码生成、执行、提交打包。

solution_design: 基于调研结果设计方案，附带 rubric 自检清单。
project_code_gen: 根据方案生成完整项目代码（model.py, dataset.py, train.py, infer.py, config）。
code_execute: 在沙箱中执行代码，捕获日志和结果。
submission_build: 按比赛格式打包预测结果。
"""

import json

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat

# 方案设计系统提示词
_SOLUTION_SYSTEM_PROMPT = """\
You are an ML competition strategist. Given task metadata, EDA report,
research summaries, and available models, design a solution approach.

Output a JSON with:
- model_selection: which model(s) to use and why
- pipeline_structure: data → preprocessing → model → postprocessing
- training_strategy: validation split, CV folds, early stopping
- loss_metric: which loss function and why
- rubric: a self-check list of criteria the solution MUST satisfy

The rubric must include at minimum: correctness, feasibility, and
submission format compliance.
"""

# 代码生成系统提示词
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


class SolutionDesignTool(BaseTool):
    """基于调研结果和数据分析设计竞赛方案。"""

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

        result_text = single_turn_chat(
            system_prompt=_SOLUTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        plan = json.loads(result_text)

        # 验证 rubric 必须存在
        if "rubric" not in plan or not plan["rubric"]:
            return ToolResult(
                success=False,
                error="Solution plan is missing required 'rubric' field. "
                      "The plan must include a self-check rubric.",
            )

        return ToolResult(data={"solution_plan": plan})


class ProjectCodeGenTool(BaseTool):
    """根据方案设计生成完整项目代码。"""

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

        code_text = single_turn_chat(
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


class CodeExecuteTool(BaseTool):
    """在沙箱中执行代码（训练或推理）。"""

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
        # TODO: 使用 athena.execution.sandbox_runtime 执行代码
        # 当前为 stub 实现
        entry_point = input["entry_point"]

        return ToolResult(
            data={
                "entry_point": entry_point,
                "status": "executed",
                "logs": "[sandbox execution stub — needs sandbox_runtime integration]",
                "output_path": f"/tmp/{entry_point}_output",
            }
        )


class SubmissionBuildTool(BaseTool):
    """按比赛格式打包预测结果。"""

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

        # LLM 驱动格式化：如果格式不匹配，让 LLM 生成转换代码
        format_prompt = (
            f"Predictions file path: {predictions_path}\n"
            f"Required submission format: {submission_format}\n\n"
            f"Generate Python code to read the predictions and write a "
            f"submission.csv file matching the required format."
        )

        format_script = single_turn_chat(
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/tools/test_baseline_builder.py -v
```

Expected: 2 passing tests（solution_design returns plan, missing rubric rejected）。

- [ ] **Step 5: Commit**

```bash
git add src/athena/tools/baseline_builder.py test/unit/tools/test_baseline_builder.py
git commit -m "feat: add baseline builder tools (solution design, code gen, execute, submit)"
```

---
### Task 9: Build TaskUnderstandAgent

**Files:**
- Create: `src/athena/agents/competition/__init__.py` (empty)
- Create: `src/athena/agents/competition/task_understand_agent.py`
- Create: `test/integration/test_task_understand_agent.py`

**Interfaces:**
- Consumes: ALL tools from Tasks 4-8, `athena.core.agent.agent.Agent`, `athena.core.tool.ToolRegistry`, `athena.core.schemas`
- Produces: `build_task_understand_agent(model: str, client: AsyncOpenAI) -> Agent` — factory function that creates a fully-configured Agent with all competition tools registered

- [ ] **Step 1: Create directories**

```bash
mkdir -p src/athena/agents/competition test/integration
```

- [ ] **Step 2: Write the agent**

```python
"""TaskUnderstandAgent — Kaggle 竞赛智能体入口。

该 Agent 是竞赛流程的唯一入口。它持有所有竞赛相关工具的 ToolRegistry，
通过 ReAct loop 自主决策工具调用顺序，完成从搜索到提交的完整 pipeline。
"""

from athena.core.agent.agent import Agent, AgentConfig, create_agent
from athena.core.tool import ToolRegistry

# 注册所有竞赛工具
from athena.tools.kaggle_search import (
    KaggleCompetitionSearchTool,
    KaggleDiscussionSearchTool,
)
from athena.tools.hf_dataset import HFDatasetSearchTool, HFDatasetDownloadTool
from athena.tools.hf_model import HFModelSearchTool, HFModelDownloadTool
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool
from athena.tools.baseline_builder import (
    SolutionDesignTool,
    ProjectCodeGenTool,
    CodeExecuteTool,
    SubmissionBuildTool,
)

# TaskUnderstandAgent 的系统提示词
_COMPETITION_SYSTEM_PROMPT = """\
You are TaskUnderstandAgent, an autonomous Kaggle competition agent.

## Your Goal
Given a competition name or URL, you autonomously complete the full
pipeline: research the competition → acquire and augment data → analyze
and clean data → design a solution → generate and execute code → produce
a submission file.

## Decision-Making Rules
1. ALWAYS start by searching for competition info using kaggle_competition_search.
2. After understanding the task, search discussions/notebooks for solution ideas.
3. Download competition data, then search HuggingFace for augmentation datasets.
4. Analyze all data before cleaning. Generate and execute cleaning code.
5. Search for and download suitable pre-trained models if applicable.
6. Design a solution with a self-check rubric BEFORE generating code.
7. Generate project code, execute training, then inference.
8. If code execution fails, analyze the error log, fix the code, and re-run.
   Maximum 3 fix-retry cycles per failure.
9. Build the submission file in the required format.

## Artifact Tracking
After each tool call, note the returned artifact references. Use them as
inputs to subsequent tools. Do not lose track of references.

## Error Recovery
When a tool returns success=False, read the error message and decide:
- Retry with adjusted parameters
- Skip optional steps (e.g. augmentation if no datasets found)
- Report to user if a required step cannot complete

## Output
When done, summarize: what was built, key metrics, submission file location.
"""


def build_task_understand_agent(
    model: str,
    client,
    *,
    max_turns: int = 30,
    max_tokens: int = 8192,
    temperature: float = 0.1,
) -> Agent:
    """构建 TaskUnderstandAgent，注册所有竞赛工具。

    Args:
        model: LLM 模型名称（如 "deepseek-v4-flash"）。
        client: OpenAI 兼容的异步客户端。
        max_turns: Agent loop 最大轮次（竞赛流程需要较多轮次）。
        max_tokens: 每次 LLM 请求的最大 token 数。
        temperature: LLM 温度参数。

    Returns:
        配置完成的 Agent 实例，可直接用于 ThreadRuntime。
    """
    # 构建工具注册表，按字母序排列以保证 prompt cache 稳定
    tools = ToolRegistry()

    # 搜索 & 理解层
    tools.register(KaggleCompetitionSearchTool())
    tools.register(KaggleDiscussionSearchTool())

    # 数据获取层
    tools.register(HFDatasetSearchTool())
    tools.register(HFDatasetDownloadTool())
    tools.register(HFModelSearchTool())
    tools.register(HFModelDownloadTool())

    # 数据准备层
    tools.register(DataAnalyzeTool())
    tools.register(DataCleanCodeGenTool())

    # 建模 & 提交层
    tools.register(SolutionDesignTool())
    tools.register(ProjectCodeGenTool())
    tools.register(CodeExecuteTool())
    tools.register(SubmissionBuildTool())

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=_COMPETITION_SYSTEM_PROMPT,
        client=client,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
        name="TaskUnderstandAgent",
        description="Autonomous Kaggle competition agent that researches, "
                    "downloads data, builds baselines, and produces submissions.",
    )
```

- [ ] **Step 3: Write integration smoke test**

```python
# test/integration/test_task_understand_agent.py
import asyncio

import pytest

# 集成测试需要真实的 LLM 访问，标记为 integration
pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="Requires real LLM API access — run manually")
@pytest.mark.asyncio
async def test_agent_loads_all_tools():
    """验证 Agent 构建成功且所有 12 个工具都已注册。"""
    from athena.agents.competition.task_understand_agent import (
        build_task_understand_agent,
    )

    agent = build_task_understand_agent(
        model="deepseek-v4-flash",
        client=None,  # 仅测试构建，不实际调用
    )
    assert agent.name == "TaskUnderstandAgent"
    assert len(agent.config.tools) == 13
    # 验证关键工具存在
    assert "kaggle_competition_search" in agent.config.tools
    assert "hf_dataset_search" in agent.config.tools
    assert "hf_model_search" in agent.config.tools
    assert "data_analyze" in agent.config.tools
    assert "solution_design" in agent.config.tools
    assert "project_code_gen" in agent.config.tools
    assert "code_execute" in agent.config.tools
    assert "submission_build" in agent.config.tools
```

- [ ] **Step 4: Run simple tests**

```bash
# 只测试 Agent 构建和工具注册（不需要 LLM）
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/integration/test_task_understand_agent.py -v -k "test_agent_loads_all_tools"
```

Expected: 1 test skipped（integration marker，需要手动激活）。

```bash
# 验证 import 正确
D:/Softwares/Miniconnda/envs/athena/python.exe -c "
from athena.agents.competition.task_understand_agent import build_task_understand_agent
print('Import OK')
"
```

Expected: `Import OK`。

- [ ] **Step 5: Commit**

```bash
git add src/athena/agents/competition/ test/integration/
git commit -m "feat: add TaskUnderstandAgent — autonomous competition agent"
```

---
### Task 10: Deprecate data_agent.py and add sample fixtures

**Files:**
- Modify: `src/athena/agents/prepare/data_agent.py` — add deprecation notice
- Create: `test/fixtures/mock_competition_page.html`
- Create: `test/fixtures/mock_discussion_kernel.ipynb`

**Interfaces:**
- Produces: Clear deprecation marker on `data_agent.py`, sample fixtures for future tests

- [ ] **Step 1: Deprecate data_agent.py**

Replace the content of `src/athena/agents/prepare/data_agent.py`:

```python
"""DEPRECATED — 此模块已被 TaskUnderstandAgent 吸收。

DataAgent 的数据分析、清洗、EDA 能力已由以下工具替代：
- athena.tools.data_prepare.DataAnalyzeTool
- athena.tools.data_prepare.DataCleanCodeGenTool

TaskUnderstandAgent 通过 ToolRegistry 持有这些工具，
不再需要独立的 DataAgent 进程。

如需数据分析能力，请使用上述工具或直接调用
athena.workflows.prepare.data_analysis 中的工作流。
"""
```

- [ ] **Step 2: Create mock competition page fixture**

```html
<!-- test/fixtures/mock_competition_page.html -->
<!-- 模拟 Kaggle 比赛页面，用于 Kaggle MCP mock 测试 -->
<!DOCTYPE html>
<html>
<head><title>Titanic - Machine Learning from Disaster | Kaggle</title></head>
<body>
<main>
    <h1>Titanic - Machine Learning from Disaster</h1>
    <section class="description">
        <h2>Description</h2>
        <p>Predict survival on the Titanic using passenger data like age, gender, and ticket class.</p>
    </section>
    <section class="evaluation">
        <h2>Evaluation</h2>
        <p>Submissions are evaluated on <strong>accuracy</strong> — the percentage of passengers you correctly predict.</p>
    </section>
    <section class="data">
        <h2>Data</h2>
        <ul>
            <li><strong>train.csv</strong> — 891 rows, 12 columns including Survived (target)</li>
            <li><strong>test.csv</strong> — 418 rows, 11 columns</li>
            <li><strong>sample_submission.csv</strong> — Example format: PassengerId, Survived</li>
        </ul>
    </section>
    <section class="submission">
        <h2>Submission Format</h2>
        <p>Submit a CSV with columns: <code>PassengerId,Survived</code></p>
    </section>
</main>
</body>
</html>
```

- [ ] **Step 3: Create mock discussion kernel fixture**

Create an empty notebook with metadata:

```bash
mkdir -p test/fixtures
```

```python
# test/fixtures/mock_discussion_kernel.ipynb
# 创建为空的 Jupyter notebook（JSON 格式）
```

Write the notebook as JSON:

```json
{
    "cells": [],
    "metadata": {
        "kaggle": {
            "kernel_type": "notebook",
            "title": "Titanic Top 3% Solution — XGBoost + Feature Engineering",
            "author": "kaggle_expert",
            "score": 0.8234,
            "url": "https://kaggle.com/c/titanic/discussion/99999"
        }
    },
    "nbformat": 4,
    "nbformat_minor": 5
}
```

- [ ] **Step 4: Commit**

```bash
git add src/athena/agents/prepare/data_agent.py test/fixtures/
git commit -m "refactor: deprecate data_agent, absorbed by TaskUnderstandAgent tools; add test fixtures"
```

---
### Task 11: End-to-end dry run and final verification

**Files:**
- No new files. This task verifies the complete system.

- [ ] **Step 1: Run all unit tests**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -m pytest test/unit/ -v
```

Expected: All unit tests pass (approximately 15 tests across 7 test files).

- [ ] **Step 2: Verify full import chain**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -c "
# 验证完整 import 链：Agent → 工具 → 工作流服务
from athena.agents.competition.task_understand_agent import build_task_understand_agent
from athena.tools.kaggle_search import KaggleCompetitionSearchTool, KaggleDiscussionSearchTool
from athena.tools.hf_dataset import HFDatasetSearchTool, HFDatasetDownloadTool
from athena.tools.hf_model import HFModelSearchTool, HFModelDownloadTool
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool
from athena.tools.baseline_builder import (
    SolutionDesignTool, ProjectCodeGenTool, CodeExecuteTool, SubmissionBuildTool
)
from athena.workflows.prepare.task_parser import parse_competition_info
from athena.workflows.prepare.dataset_service import create_data_card

print('All imports OK')
print(f'Tools: {12} total')
"
```

Expected: `All imports OK`, `Tools: 13 total`.

- [ ] **Step 3: Manual smoke test (optional — requires LLM API)**

```bash
D:/Softwares/Miniconnda/envs/athena/python.exe -c "
import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI
from athena.agents.competition.task_understand_agent import build_task_understand_agent
from athena.core.agent.agent import AgentContext
from athena.core.schemas import AthenaThread, AthenaTurn
import os

async def smoke_test():
    load_dotenv()
    api_key = os.getenv('DEEPSEEK_API_KEY')
    if not api_key:
        print('SKIP: No DEEPSEEK_API_KEY set')
        return
    client = AsyncOpenAI(api_key=api_key, base_url='https://api.deepseek.com')
    agent = build_task_understand_agent('deepseek-v4-flash', client)
    print(f'Agent built: {agent.name}')
    print(f'Registered tools ({len(agent.config.tools)}):')
    for spec in agent.config.tools.specs:
        print(f'  - {spec.name}')
    await client.close()

asyncio.run(smoke_test())
"
```

Expected: Lists all 12 tool names.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete TaskUnderstandAgent implementation — 13 tools registered"
```

---

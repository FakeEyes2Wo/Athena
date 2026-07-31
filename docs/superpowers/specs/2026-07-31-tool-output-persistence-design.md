# 工具输出直接落盘 设计

**Date:** 2026-07-31
**Author:** YeBai
**Status:** Approved — ready for implementation plan

## 1. 问题陈述

当前 TaskUnderstandAgent 的 13 个工具执行后，产物（生成的代码、EDA 报告、方案设计等）仅作为字符串存在于 LLM 对话历史中，并未写入文件系统。真正落盘发生在后续读取对话历史并手动提取的阶段。这导致：

- 生成的代码无法直接执行（`code_execute` 拿不到实际文件）
- 调试时需要从对话历史中手动复制粘贴产物
- 工具链之间无法通过文件路径传递产物

**目标**：每个工具在执行阶段直接将产物写入文件系统，`ToolResult` 中返回文件路径。

## 2. 设计决策

### 2.1 构造器注入，不改公共接口

遵循项目中 `PaperMarkdownTool(store, ...)` 和 `PaperSourceTool` 的既有模式：工具通过构造器接收依赖，不修改 `ToolContext`、`BaseTool`、`AgentContext` 等公共类型。

```python
# 每个工具新增 __init__
class ProjectCodeGenTool(BaseTool):
    def __init__(self, work_root: str):
        self.output_dir = Path(work_root) / "project_code_gen"
```

`build_task_understand_agent()` 新增 `work_root` 参数，统一传递给所有工具：

```python
def build_task_understand_agent(
    model: str,
    client: "AsyncOpenAI | None" = None,
    *,
    work_root: str = "work",   # 新增
    max_turns: int = 30,
    ...
) -> Agent:
```

调用方（测试脚本或 app server）自行决定 `work_root`。整个竞赛流程共用一个 work 目录。

### 2.2 不改动的文件

| 文件 | 原因 |
|---|---|
| `core/tool.py` | BaseTool 保持最小，不引入文件系统概念 |
| `core/tool_types.py` | ToolContext 保持四字段，不增加 output_dir |
| `core/agent/agent.py` | Agent 层不关心工具如何持久化 |
| `app_server/thread_runtime.py` | work_root 由调用方决定，不进入运行时内核 |

## 3. 目录结构

```
work/                          # work_root
├── kaggle_competition_search/ # competition_info.json
├── kaggle_discussion_search/  # discussions.json
├── kaggle_dataset_download/   # 数据集文件 + datacard.json
├── hf_dataset_search/         # search_results.json
├── hf_dataset_download/       # 数据集文件 + datacard.json
├── hf_model_search/           # search_results.json
├── hf_model_download/         # 模型权重
├── data_analyze/              # eda_report.json
├── data_clean_code_gen/       # clean_script.py
├── solution_design/           # solution_plan.json
├── project_code_gen/          # model.py, dataset.py, train.py, infer.py, config.yaml
├── code_execute/              # train_log.txt, infer_log.txt, checkpoint/, predictions/
└── submission_build/          # format_script.py, submission.csv
```

同一个工具多次调用时，后一次覆盖前一次。

## 4. 落盘详情

### 4.1 搜索 & 理解层

| 工具 | 落盘文件 | 格式 |
|---|---|---|
| `kaggle_competition_search` | `competition_info.json` | `json.dumps(TaskMetaData)` |
| `kaggle_discussion_search` | `discussions.json` | `json.dumps(讨论列表)` |
| `kaggle_dataset_download` | 数据集原样 + `datacard.json` | 下载内容 + JSON 元信息 |

### 4.2 数据获取层 — HF 数据集

| 工具 | 落盘文件 | 格式 |
|---|---|---|
| `hf_dataset_search` | `search_results.json` | `json.dumps(搜索结果)` |
| `hf_dataset_download` | 数据集原样 + `datacard.json` | 下载内容 + JSON 元信息 |

### 4.3 数据获取层 — HF 模型

| 工具 | 落盘文件 | 格式 |
|---|---|---|
| `hf_model_search` | `search_results.json` | `json.dumps(搜索结果)` |
| `hf_model_download` | 模型权重原样 | 下载内容 |

### 4.4 数据准备层

| 工具 | 落盘文件 | 说明 |
|---|---|---|
| `data_analyze` | `eda_report.json` | LLM 输出已是 JSON，直接 `json.dump` |
| `data_clean_code_gen` | `clean_script.py` | 从 LLM 文本响应中提取 Python 代码块，提取失败则返回 `ToolResult(success=False)` |

### 4.5 建模 & 提交层

| 工具 | 落盘文件 | 说明 |
|---|---|---|
| `solution_design` | `solution_plan.json` | LLM 输出已是 JSON（`response_format={"type": "json_object"}`），直接 `json.dump` |
| `project_code_gen` | `model.py`, `dataset.py`, `train.py`, `infer.py`, `config.yaml` | 从 LLM 文本响应中解析各文件，解析失败则返回 `ToolResult(success=False)` |
| `code_execute` | `train_log.txt` / `infer_log.txt` + `checkpoint/` / `predictions/` | 根据 `entry_point` 决定输出内容；当前为占位 stub，落盘逻辑先写好 |
| `submission_build` | `format_script.py` + `submission.csv` | `format_script.py` 从 LLM 响应提取；`submission.csv` 待后续实现格式转换执行 |

## 5. 代码解析规则

对于 LLM 输出为自由文本（非 JSON）的工具，需要从响应中提取结构化内容：

### 5.1 单脚本工具（data_clean_code_gen, submission_build）

从 Markdown 代码块（`` ```python ... ``` ``）中提取。规则：
- 优先提取第一个 `python` 语言标记的代码块
- 没有标记时回退到第一个任意代码块
- 都没有则返回 `ToolResult(success=False, error="No code block found in LLM response")`

### 5.2 多文件工具（project_code_gen）

从 LLM 响应中按文件标记解析。支持两种格式：
- Markdown 代码块 + 文件名注释：`` ```python  # model.py ``
- 文件名标题 + 代码块：`### model.py` 后跟代码块

解析结果必须包含全部 5 个文件（model.py, dataset.py, train.py, infer.py, config.yaml），缺一不可。缺失任何文件则返回 `ToolResult(success=False)`。

### 5.3 JSON 输出工具

`solution_design` 和 `data_analyze` 已使用 `response_format={"type": "json_object"}`，LLM 返回合法 JSON，`json.loads` 成功后直接 `json.dump` 写入。

## 6. ToolResult 返回值变化

落盘后，`ToolResult.data` 中增加 `output_dir` 字段，指向本工具写入的目录路径，供 Agent 后续推理引用：

```python
return ToolResult(data={
    "files": ["model.py", "dataset.py", "train.py", "infer.py", "config.yaml"],
    "output_dir": str(self.output_dir),
    # 保留原有字段
    "solution_plan_ref": input["solution_plan_ref"],
    "status": "code_generated",
})
```

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| 目录创建失败（权限不足） | 返回 `ToolResult(success=False, error=...)` |
| LLM 响应解析失败（无代码块/缺文件） | 返回 `ToolResult(success=False, error=...)` |
| 文件写入失败（磁盘满） | 返回 `ToolResult(success=False, error=...)` |
| JSON 序列化失败 | 返回 `ToolResult(success=False, error=...)` |

所有错误均不抛异常，通过 `ToolResult(success=False)` 返回，让 Agent 的 ReAct loop 决定重试或跳过。

## 8. 测试策略

已有测试（`test/unit/tools/` 下）的 mock 不受影响——工具输出仍然是 `ToolResult`，只是多了一个 `output_dir` 字段。

新增测试用例：
- 每个工具的 `output_dir` 在构造后正确拼接
- JSON 输出工具：文件落盘后内容与 `ToolResult.data` 一致
- 代码生成工具：正常 LLM 响应 → 文件落盘；无代码块响应 → `success=False`
- `project_code_gen`：全部 5 文件 → 成功；缺文件 → `success=False`

## 9. 依赖

无新依赖。使用标准库 `json`、`pathlib.Path`、`re`。

## 10. 不改动的工具

`submission_build` 的内部逻辑不改动，仅添加落盘。`code_execute` 当前是占位 stub，落盘逻辑先写好，实际沙箱执行留待后续集成。

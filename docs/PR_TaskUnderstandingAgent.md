# TaskUnderstandingAgent — 竞赛 Agent 工具链实现

## 概述

实现 Kaggle 竞赛任务理解部分的Agent：**TaskUnderstandAgent**，以及完成最小闭环测试所需的全部工具链（13 个工具，部分工具设计是草稿形态，后续应根据设计增减）。通过 HuggingFace Hub 完成数据集/模型搜索与下载，支持 EDA 分析、方案设计、代码生成和提交打包。

---

## 已完成

### 1. 工具链（13 个工具注册于 TaskUnderstandAgent）

| 层级 | 工具 | 状态 |
|------|------|------|
| 竞赛搜索 | `kaggle_competition_search` | MCP stub 已就绪，待接入实际 MCP 服务 |
| | `kaggle_discussion_search` | MCP stub 已就绪 |
| | `kaggle_dataset_download` | MCP stub 已就绪 |
| HF 数据集 | `hf_dataset_search` | 线上测试通过 |
| | `hf_dataset_download` | 线上测试通过 |
| HF 模型 | `hf_model_search` | 线上测试通过 |
| | `hf_model_download` | 线上测试通过 |
| 数据准备 | `data_analyze` | 扫描真实数据目录，读样本，LLM 生成 EDA 报告（逻辑仍有问题，设计需要变更） |
| | `data_clean_code_gen` | 基于 EDA 报告生成清洗脚（逻辑仍有问题，设计需要变更） |
| 建模提交 | `solution_design` | 仅为完成最小闭环测试的草稿，等待其他人完成最终设计|
| | `project_code_gen` | 仅为完成最小闭环测试的草稿，等待其他人完成最终设计|
| | `code_execute` |仅为完成最小闭环测试的草稿，等待其他人完成最终设计|
| | `submission_build` |仅为完成最小闭环测试的草稿，等待其他人完成最终设计 |

所有工具执行产物直接落盘到 `{work_dir}/{tool_name}/` 子目录，不依赖对话历史提取。

### 2. TaskUnderstandAgent

- 注册全部 13 个工具，ReAct loop 自主决策工具调用顺序
- 并发策略：`concurrency_safe` 工具并行执行，非安全工具（落盘类）串行化
- 端到端 demo 脚本：`demo_taskunderstand_agent.py`
  - `--mode seeded`：绕过 Kaggle stub，预注入竞赛元数据
  - `--mode auto`：完全自主 ReAct 模式
  - `--hf-endpoint` 支持 HuggingFace 镜像端点

### 3. 工具链前置依赖校验

下游工具在缺少前置产物时返回明确错误，防止 LLM 在没有数据的情况下虚构结果：

| 工具 | 校验逻辑 |
|------|----------|
| `data_analyze` | 扫描 `work_root` 一级子目录，无数据文件时返回 `"No data files found"` |
| `data_clean_code_gen` | 校验 `data_analyze/eda_report.json` 存在，否则拒绝 |
| `project_code_gen` | 校验 `solution_design/solution_plan.json` 存在，否则拒绝 |
| `submission_build` | 校验预测文件路径存在，否则拒绝 |

### 4. 种子模式（Seeded Mode）

通过 `_TITANIC_SEEDED_PROMPT` 预注入竞赛元数据，在不依赖 Kaggle MCP 的前提下完成端到端 pipeline 验证。引导 LLM 分阶段执行（获取数据 → 分析 → 设计 → 生成代码），防止 LLM 在没有数据时批量调用下游工具。

---

## 未完成（后续 PR 计划）

- **Kaggle MCP 服务接入**：3 个 kaggle 工具当前为 stub，demo 通过 seeded 模式绕过。待接入实际 MCP 服务 `https://www.kaggle.com/docs/mcp`。
- **data_prepare 流程优化**：当前 EDA 基于扫描目录 + 样本传给 LLM 的方式能用，但理想方案应包含自主抽样策略设计、多轮分析 Loop 和画图 Agent。


---

## 测试

- 全部工具含单元测试（`test/unit/tools/`）
- Titanic 竞赛端到端线上测试通过（真实 LLM API + HuggingFace Hub 调用）通过```python demo_taskunderstand_agent.py```调用最小闭环测试

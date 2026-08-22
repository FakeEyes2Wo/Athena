# PREPARE 阶段：dataclean agent + PR7 工具移植 + 全 PREPARE E2E 设计

日期：2026-08-22
状态：待评审

## 背景与目标

自动化科研系统 Athena 的 PREPARE 阶段目前有 evaluator、EDA、baseline_ideator、prepare
四个 agent（协作方已完成）。本设计完成两件事并附带一个端到端测试：

1. 新增 **dataclean agent**：分析已下载/给定的数据，按数据集实际情况选择清洗方法
   （可能需要编写并执行代码），工作目录 `workspaces/dataclean`，并接入
   `run_prepare_phase` 加入 PREPARE 阶段。
2. 将 PR7 分支里的 **HuggingFace 工具 + MCP 工具**（`pr7_review/src/athena/tools`）移植到
   main，并接入 dataclean、evaluator、baseline_ideator 三个 agent 的 `extra_tools`。
3. 新增一个**模拟前端 prompt 进入整个 PREPARE 阶段的 E2E 测试**。

## 决策记录

以下决策此前已与用户确认（AskUserQuestion / 对话），**必须遵守**：

| 决策点 | 结论 |
|---|---|
| dataclean 在 PREPARE 中的位置 | **evaluator 冻结之后、EDA 之前** |
| MCP 范围 | **只移植工具层，先不配置 server**（MCP 休眠，HF 工具可用） |
| ideator 范围 | **只给 baseline_ideator** 加 HF+MCP（不给 SEARCH ideators） |
| dataclean 形态 | evaluator 式修复循环（PlanDecision + 迭代到验证通过）；成功时输出 `DATACLEAN_HANDOFF.md` |
| dataclean 完成判定 | **数据无关**：不依赖任何先验的数据集结构（本次新增修正） |
| E2E 修复轮 | **选项 2**：dataclean 在 E2E 里演示一个"坏产物 → 反馈 → 修复 → 提交"的回合 |
| E2E 数据 | **关闭 Kaggle，用本地数据**：在 tmp 项目里种一份模拟真实
  `filament-segmentation-2026`（MAGFiLO）形状的合成数据集；任务文本指向本地路径 |

真实数据集形状（仅作为合成数据与 prompt 的背景参考，不是完成判定的依据）：
`D:\WorkRoot\kaggle_solar_run_01\filament-segmentation-2026`——COCO 格式实例分割，
`train/train_images/*.jpeg`（2048×2048）+ `MAGFiLO_1.0_Annotations_kaggle2026_train.json`
（images + annotations：segmentation 多边形 / spine / bbox，4 类 filament），
`test/test_images/*.jpeg`。

---

## Part A：dataclean agent

### A1. 数据无关的清洗方法论（prompt 核心）

`dataclean_agent.md` 不写死任何数据集结构，而是规定一套**先勘察、按证据决策、自描述**的流程：

1. **勘察（INSPECT）**：枚举数据集的组成（文件、大小、格式）；读取 schema / 元数据
   （表头、标签文件、标注结构）；抽样读内容；量化问题（缺失、重复、损坏文件、标签语法
   不一致、越界值、类别失衡、格式不匹配）。**严禁假设布局，一切以实际勘察为准。**
2. **决策（DECIDE）**：基于勘察到的证据选择清洗方法（丢弃/填补、去重、重格式化、重采样、
   过滤劣质记录、修复引用等），每条决策必须引用证据。**数据本身已干净时明确说"无需清洗"，
   不硬造工作。**
3. **实现并运行（IMPLEMENT + RUN）**：把清洗写成可复现、依赖轻的脚本（如 `clean.py`）
   放进工作区并执行；验证清洗后的数据能被正确读回。
4. **自描述（DOCUMENT）**：写 `DATACLEAN_HANDOFF.md`——数据概览、发现的问题、逐条决策与
   理由、产出布局（文件 + 格式）、下游如何消费清洗后的数据、如何复现清洗脚本。**不允许声称
   没有真正执行的清洗。**

输出契约：`PlanDecision`（`continue|submit|abandon`）。仅当 `DATACLEAN_HANDOFF.md` 存在且
如实描述实际完成的工作时才 `submit`。

### A2. 完成 gate（数据无关）

`run_dataclean_plan` 镜像 `run_evaluator_plan` 的修复循环（create/followup →
`wait_run_events` → `_decision_from_summary` → 校验 → 反馈/提交）。校验只检查数据无关的产物：

- `workspaces/dataclean/DATACLEAN_HANDOFF.md` 存在且非空（即 agent 真正分析过数据并写下了
  结论）；
- decision == `submit`。

**不校验任何固定输出文件/目录名、不解析 handoff 内容、不按数据集类型特判**——个性化决策的
对错由 agent 的勘察与 handoff 负责，`run_dataclean_plan` 只负责"确实有产物 + 明确提交"。
`abandon` → 抛错；turn budget 耗尽 → 抛错。

返回：`DATACLEAN_HANDOFF.md` 的文本（供下游 EDA 引用清洗数据位置）。

### A3. 代码结构

- `src/athena/agents/prepare_dataclean_agent.py`（新增，命名沿用 evaluator/EDA 的
  `prepare_*_agent` 系列）：
  `DATACLEAN_AGENT_ID = DATACLEAN_AGENT_TYPE = "dataclean"`；
  `register_dataclean_agent(registry, *, provider, artifacts, workspace, runtime,
  extra_tools)` → 调用 `register_prompt_agent(..., output_type=PlanDecision, ...)`。
  与 `prepare_agent.py` 里 `register_evaluator_agent` 完全同构。
- `src/athena/core/agent/prompts/dataclean_agent.md`（新增）：见 A1。
- `src/athena/research/supervisor/prepare.py`：新增 `run_dataclean_plan`（镜像
  `run_evaluator_plan`）与 `DATACLEAN_AGENT_ID/PLAN_ID` 常量；导出。
- `src/athena/kaggle/wiring.py`：`AGENT_KAGGLE_TOOLS["dataclean"] = (KAGGLE_GET_COMPETITION,
  KAGGLE_DOWNLOAD_DATA)`（真实运行时 dataclean 也能用 Kaggle 工具；E2E 关闭 Kaggle）。

### A4. phase_runner 接线（最小改动）

在 `run_prepare_phase` 的"步骤 1 evaluator 冻结"之后、"步骤 2a EDA"之前插入"步骤 1b"：

```
dataclean_dir = rt._workspaces_root / "dataclean"
若 DATACLEAN_HANDOFF.md 已存在 → 发布"复用 dataclean 断点"并读取文本（断点复用）
否则：
  register_dataclean_agent(..., extra_tools=rt.agent_tools("dataclean"))
  handoff_text = await run_dataclean_plan(...)
```

EDA orchestrator 的 content 追加一行说明清洗数据位置（指向 `dataclean_dir` 与
`DATACLEAN_HANDOFF.md`），让 EDA/baseline 能消费清洗后的数据。改动均为新增行，
不动既有 evaluator/EDA/prepare 的逻辑。

### A5. 断点复用

与 evaluator 的 checkpoint 不同，dataclean 以"产物文件存在"为断点：
`workspaces/dataclean/DATACLEAN_HANDOFF.md` 存在即复用（数据无关，无需 state 字段）。

---

## Part B：PR7 工具移植

### B1. 移植范围

从 `pr7_review/src/athena/tools` 原样移植到 `src/athena/tools`：

- `hf_dataset.py`：`HFDatasetSearchTool` / `HFDatasetDownloadTool`
- `hf_model.py`：`HFModelSearchTool` / `HFModelDownloadTool`
- `mcp/__init__.py`（`register_mcp_tools`）、`mcp/config.py`（`McpServerConfig` /
  `load_mcp_servers`）、`mcp/client.py`（`McpClientManager`）、`mcp/adapter.py`
  （`McpToolAdapter`）、`mcp/search.py`（`McpSearchTools`）

按 `docs/代码规范.md` 对齐（导入顺序、docstring、无 ASCII 分隔线、去 `print` 噪音——HF
工具里的终端 print 视情况保留或移除）。

### B2. 依赖

`pyproject.toml` 新增：`huggingface_hub`、`mcp`、`httpx`（当前环境已确认有 `mcp`、
`httpx 0.28.1`，缺 `huggingface_hub`）。安装到 `.venv`。

### B3. 运行时装配（runtime.py，最小改动）

- `hf_tools(work_root) -> ToolRegistry`（同步）：把 4 个 HF 工具绑定到
  `work_root = self._root / "workspaces" / "pr7_tools"`。
- MCP 装配（async，休眠路径）：`async def init_mcp_tools(self)` 幂等——从
  `self._root / "mcp_servers.json"` 读配置，非空才 `register_mcp_tools`；结果缓存进
  `self._mcp_registry` / `self._mcp_managers`。**仓库根放一个空配置
  `{"servers": []}`**，因此现在这是廉价 no-op。`aclose()` 里关闭 managers（当前为空）。
- `agent_tools(agent_type) -> ToolRegistry | None`（同步）：`_merged(kaggle_tools(agent_type),
  hf_tools(), self._mcp_registry)`，供 evaluator / dataclean / baseline_ideator 用。
- `baseline_ideator_tools() -> Callable[[], ToolRegistry | None]`：保留现有 `ideator_tools()`
  的懒加载语义（Kaggle + 语料按就绪时间接入），再并入 HF + 缓存的 MCP registry。**SEARCH
  ideators 的 `ideator_tools()` 保持不变。**
- `register_prompt_agent` 工厂：不改。MCP 休眠时 `_mcp_registry` 为空，`McpSearchTools` 不会
  出现在任何 extra_tools 里；将来配置 server 时若要走 `extra_tools` 拷贝语义，需要给
  `McpSearchTools` 加 `bind_to_registry` 钩子——**记入 docstring/TODO，不在本期实现**。

### B4. 接线

- evaluator / dataclean：`extra_tools=rt.agent_tools("evaluator" | "dataclean")`
- baseline_ideator：`extra_tools=rt.baseline_ideator_tools()`
- `run_prepare_phase` 顶部调用一次 `await rt.init_mcp_tools()`（幂等、今日 no-op）。

---

## Part C：测试

### C1. dataclean 单 agent 测试（新增，复用 `test_prepare_agent_contract.py` 的模式）

- `run_dataclean_plan` 正常提交（首轮写 DATACLEAN_HANDOFF.md → submit）；
- **修复轮**：首轮只写空/占位 handoff → gate 反馈 → 次轮补全 → submit（证明修复回路）；
- `abandon` → 抛错；缺 handoff 时 budget 耗尽 → 抛错；
- 数据无关性：对**不同形状**的两个数据集（一个 COCO 标注结构、一个纯表格 CSV），fake 分别
  返回贴合各自结构的脚本，验证 gate 不因数据形状而卡住/放行错误。

### C2. PR7 工具测试（移植 `pr7_review/test` 中对应用例）

- HF 工具：mock `HfApi` / `snapshot_download`，验证 search/download 的落盘与返回。
- MCP：配置加载（缺文件 → 空列表）、占位符展开、空配置 no-op。

### C3. 全 PREPARE E2E（`test/integration/research/test_prepare_phase_e2e.py`，新增）

**目标**：用假 LLM 客户端驱动真实组合根，模拟前端 prompt 进入 PREPARE 并跑完全部 agent，
证明编排 + handoff 契约 + 文件链路全串通。修复行为由单 agent 测试（C1 + 既有）覆盖。

**合成本地数据**：在 `tmp_path` 种一份形状贴合 MAGFiLO 的微缩数据集——
`train_images/`（几张小的合法 jpeg）+ `MAGFiLO_1.0_Annotations_kaggle2026_train.json`
（COCO images + annotations，1–2 个多边形）+ `test_images/`（1 张）。任务文本：
`"我要参加 solar filament segmentation 2026 比赛，本地数据在 <该目录>"`。
（committed 测试用合成数据保证 CI 可复现；对真实 700MB 数据集的运行是手动/开发期行为，
不进测试。）

**agent-aware 假客户端**：替换 `_support.FakeStreamingClient` 同一接缝
（`client.chat.completions.create`），按 system prompt 的 agent 标记分发，每 agent 一份
"按 turn 计数的动作队列"（复用 `_EvaluatorProvider` 的模式）。标记未命中即抛错。

各 agent 脚本（**E2E 只走 happy-path 首轮 + dataclean 一个修复轮**）：

| agent | 脚本 |
|---|---|
| SupervisorAgent | ①`configure_kaggle {"enabled":false}` ②`record_task_understanding` ③`{"answer":...}` |
| Evaluator | ①write metric.json/pyproject/evaluate.py/labels.csv/HANDOFF.md ②`{decision:submit}` |
| DataClean | ①write 占位 DATACLEAN_HANDOFF.md（空/仅标题）→ gate 反馈 ②write 完整 handoff ③`{decision:submit}` |
| EDA orchestrator | ①write EDA_TODO.md ②finalize EDA_INDEX.md/EDA_HANDOFF.md → `HandoffResult` |
| eda-worker | ①write EDA_REPORT_XX.md → `HandoffResult` |
| baseline_ideator | ①write BASELINE_DESIGN.md → `HandoffResult` |
| PREPARE Agent | ①write solution/* + experiment.json ②`{decision:submit}` |
| Kaggle Handoff（SEARCH 预算 0 时可能触发一次） | ①write KAGGLE_HANDOFF.md → `KaggleHandoffResult` |

**入口与停点**：`runtime.start_task(task)`（前端 RPC 入口），构造
`ResearchRuntime(..., search_limit=0, auto_validate=False)`（`survey` 默认关）。PREPARE
跑完后 SEARCH 预算为 0、`auto_validate=False` 停在 WAITING；测试用 `_eventually(...)`
等 PREPARE 完成，断言后 `aclose()`。

**真实 vs 桩**：LLM 假；文件 IO / git / artifact store / baseline 脚本执行
（stdlib `model.py`）真；evaluator freeze 的 uv lock/sync 与既有
`test_prepare_agent_contract.py` 现状一致。**`@pytest.mark.slow`**，默认 `-m 'not slow'`
不跑。

**断言**：evaluator 冻结（metric.json/evaluate.py/labels.csv）、
`workspaces/dataclean/DATACLEAN_HANDOFF.md` 存在且非空、EDA_INDEX/HANDOFF、
BASELINE_DESIGN.md、`state` 有 trusted baseline（SOTA + metric）。

---

## 工程约束（用户明确要求，全程遵守）

1. 遵循 `docs/代码规范.md`。
2. 避免复杂、冗余实现；代码轻量、工程化、低维护成本。
3. 避免在已有文件里大量改动（协作方共同维护）；仅最小必要改动——dataclean 接线只新增
   phase_runner 一小段 + prepare.py 一个函数；runtime.py 只加装配方法；`register_prompt_agent`
   不改。

## 里程碑顺序

1. Part A（dataclean agent + 接线 + 单 agent 测试）
2. Part B（PR7 工具移植 + 依赖 + 运行时装配 + 工具测试）
3. Part C3（E2E 全 PREPARE 测试）

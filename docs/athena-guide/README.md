# Athena 开发者指南

这组文档面向第一次接触 Athena 的开发者，内容以当前 Python 生产实现为准。本文先回答“项目做什么、怎样流转、从哪里修改”这几个问题；需要了解实现细节时，再进入后面的专题章节。Rust 目录是平行实现，不是当前默认运行链路。

## 1. 项目解决什么问题

Athena 是一个面向 AI4ML / AI4S 的自动研究系统。用户提供自然语言研究任务和数据，系统负责把一次开放式机器学习研究组织成可追踪、可恢复的工程流程：

- 理解任务并在信息不足时向用户澄清；
- 探索数据，建立可信的评估器与 baseline；
- 生成有依据的改进假设，在隔离工作区中执行实验；
- 用同一评估口径比较实验，持续维护当前 SOTA；
- 对最佳结果做最终验证，保留研究树、状态、证据和报告。

它主要解决的不是“调用一次模型生成代码”，而是自动研究中的连续决策问题：谁可以修改全局状态、实验怎样隔离、结果如何公平比较、失败后怎样恢复，以及后续 Agent 怎样继承前面阶段的证据。

核心约束是单一写者：`ResearchRuntime` 负责装配依赖，`Supervisor` 负责推进流程并统一修改 `ResearchState` 与 `ResearchTree`。Agent、工具和评估器执行具体工作，但不绕过 Supervisor 随意改写全局研究状态。

## 2. 整体工作流程

```text
自然语言任务 + 数据路径
  → 任务理解与必要的多轮澄清
  → PREPARE：EDA、冻结评估器、建立可信 baseline
  → SEARCH：生成假设、创建实验工作区、执行与评分、更新 SOTA
  → VALIDATE：复现并验证冻结的最佳结果
  → COMPLETED：持久化状态、研究树、证据与最终报告
```

各阶段的职责如下：

| 阶段 | 主要工作 | 关键产物 |
|---|---|---|
| 任务理解 | 解析目标、指标、约束；必要时通过 GUI 选择题继续澄清 | `task_understanding`、`task_clarification` handoff |
| `PREPARE` | 探索数据，生成 EDA，冻结可信评估器并运行 baseline | EDA 工作区、evaluator artifact、baseline/SOTA |
| `SEARCH` | Ideator 提出假设，Scheduler 填充并发槽位，Plan Agent 在独立工作区实验 | 假设、实验、评分证据、更新后的 SOTA |
| `VALIDATE` | 对冻结的 SOTA 做最终复现和验证 | validation 结果、最终报告 artifact |
| `COMPLETED` | 发布终态和报告，保留可恢复、可审计的运行记录 | `state.json`、`research_tree.json`、artifacts、logs |

主调用链可以简化为：

```text
CLI / TUI / GUI / headless
  → ResearchRuntime
  → Supervisor
  → PhaseMachine
  → PhaseRunner / SearchLoop / PlanLifecycle / AgentTurnRunner
  → AgentRuntime + Tools + ExecutionRuntime + TrustedEvaluator
  → ResearchTree + ResearchState + ArtifactStore + RuntimeEvents
```

完整的阶段状态机、断点续传和边界情况见 [06 当前工作流架构](06-workflow.md)。

## 3. 核心模块如何划分

| 层次 | 目录或对象 | 职责 |
|---|---|---|
| 交互入口 | `src/athena/cli.py`、`src/athena_tui/`、`athena-gui/`、`scripts/run_headless.py` | 接收任务、展示事件、发出暂停/恢复等控制命令 |
| GUI 网关 | `src/gui_gateway/`、`src/athena/gui/` | 在 WebSocket RPC、GUI 查询模型与 `ResearchRuntime` 之间做适配 |
| 组合根 | `src/athena/research/runtime/facade.py` | 一次性装配模型 Provider、Agent、工具、执行器、评估器、工作区、状态和事件系统 |
| 流程控制 | `src/athena/research/supervisor/` | 阶段推进、搜索调度、Plan 生命周期、恢复和全局状态写入 |
| 阶段编排 | `src/athena/research/runtime/phase_runner.py`、`turns/ideator.py` | 把阶段动作落实为具体 Agent turn、handoff 和评估调用 |
| Agent 实现 | `src/athena/agents/` | Supervisor、Prepare、EDA、Ideator、Plan、Validate、General 等业务 Agent 的注册与行为 |
| Agent 基建 | `src/athena/core/agent/` | Agent 注册表、运行循环、Provider、Session、Mailbox、Prompt 和 Agent 工具 |
| 领域模型 | `src/athena/core/research_tree.py`、`research_models.py` | 表达假设、实验、评估结果和 SOTA 关系 |
| 执行与隔离 | `src/athena/execution/`、`src/athena/core/git_workspace.py` | 执行命令/脚本，并为实验创建隔离工作区 |
| 持久化与事件 | `core/artifact_store.py`、`supervisor/state.py`、`research/runtime/events.py` | 保存内容寻址工件、运行快照、断点信息和前端事件 |
| 可选研究能力 | `src/athena/research/literature/survey/`、`src/athena/kaggle/` | 论文检索与语料构建、Kaggle 数据和提交链路 |
| 协议与平行实现 | `src/athena/app_server/`、`athena-rust/` | Thread/Turn 协议基础设施及 Rust 平行实现 |

理解整个系统时，建议先抓住四个边界：`ResearchRuntime` 是组合根，`Supervisor` 是全局状态唯一写者，`ResearchTree` 是研究事实记录，artifact ref 是大型文本和证据在模块间传递的主要方式。

## 4. 项目如何运行

### 4.1 准备环境

要求 Python 3.11 或更高版本，并安装 [uv](https://docs.astral.sh/uv/)。在仓库根目录执行：

```powershell
uv sync
Copy-Item .env.example .env
```

然后在 `.env` 中至少配置 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`。模型、Provider、base URL 等非密钥设置见 `config.example.toml`。

### 4.2 CLI：自动跑完整流程

```powershell
uv run Athena-cli run `
  --project .athena/titanic-run `
  --data examples/titanic/train.csv `
  --task "预测泰坦尼克号乘客是否存活" `
  --mode auto
```

查看和控制已有运行：

```powershell
uv run Athena-cli status --project .athena/titanic-run
uv run Athena-cli pause  --project .athena/titanic-run
uv run Athena-cli resume --project .athena/titanic-run
uv run Athena-cli stop   --project .athena/titanic-run
```

### 4.3 TUI：终端交互运行

```powershell
uv run Athena-tui --project .athena/tui-run
```

TUI 必须在交互式终端中运行。启动后输入自然语言任务；`/pause`、`/resume`、`/stop` 用于控制流程。

### 4.4 GUI：浏览器开发模式

先完成根目录的 `uv sync`，再执行：

```powershell
Set-Location athena-gui
npm install
npm run dev:web
```

`dev:web` 会同时启动 Vite 前端和 `.venv` 中的 Python `gui_gateway`，默认使用 `ws://127.0.0.1:17601`。只启动后端可运行 `npm run backend`；Tauri 桌面开发模式使用 `npm run tauri dev`，并额外要求 Rust/Tauri 工具链。

### 4.5 Headless：脚本或 CI

```powershell
uv run python scripts/run_headless.py `
  --project .athena/headless-run `
  --task "构建分类 baseline 并继续改进" `
  --data (Resolve-Path examples/titanic/train.csv) `
  --search-limit 2
```

Headless 会订阅 `state` / `output` 事件并一直运行到终态。数据路径建议使用绝对路径，避免实验工作区切换后无法定位源数据。

## 5. 关键输入和输出

### 5.1 输入

| 输入 | 形式 | 说明 |
|---|---|---|
| 研究任务 | 自然语言字符串 | 描述目标、目标列、指标、约束和期望交付物；越明确，任务理解阶段越稳定 |
| 数据 | 文件或目录路径 | CLI 的 `--data` 必填，headless 可选；数据本身不会因为传入路径就自动复制到每个实验目录 |
| 运行参数 | CLI 参数或 GUI 设置 | 搜索预算、并发度、ideator 数量、评估方向、自动验证等 |
| 模型配置 | `.env`、`config.toml` | API key、Provider、模型名和兼容端点 |
| 可选外部来源 | Survey / Kaggle 配置 | 论文语料、竞赛数据、额外凭据与下载目录 |
| 人类回复 | GUI/TUI 消息 | 任务澄清、预算追加、进入验证或停止等决策 |

### 5.2 输出

若 `--project` 指向 `<project>`，默认运行数据布局为：

```text
<project>/
├── .athena/
│   ├── state.json             # 核心运行状态
│   ├── resume.json            # 与 state digest 绑定的断点续传字段
│   ├── research_tree.json     # 假设、实验、评估和 SOTA
│   ├── artifacts/             # 内容寻址的报告、证据、handoff、预测等
│   ├── logs/agents/           # Agent rollout
│   ├── logs/sessions/         # 事件/会话记录
│   ├── runs/                  # 数据脚本执行目录
│   └── repo/                  # 实验用本地 Git 仓库
└── workspaces/                # EDA 与实验隔离工作区
```

需要特别注意：

- 最终报告由 `build_final_report()` 生成并写入 artifact store，引用保存在 `state.validation.report_ref`；它不保证以固定的 `final_report.md` 文件名存在。
- `TASK_CLARIFICATION.md` 是逻辑文档名称。当前实现把其 Markdown 内容写成 artifact，并通过 `handoff_refs["task_clarification"]` 传给后续 Agent。
- `research_tree.json` 记录研究事实和 SOTA 关系，`state.json` 记录流程状态；两者职责不同，不应互相替代。
- 运行时只向前端发布 `state` 和 `output` 两类事件。CLI/TUI/GUI 只是用不同方式消费同一事件流。

## 6. 主要代码入口在哪里

| 场景 | 入口 | 下一跳 |
|---|---|---|
| CLI | `src/athena/cli.py::main` | `_dispatch_command()` → `_cmd_run()` → `ResearchRuntime` |
| TUI | `src/athena_tui/entrypoint.py::main` | `AthenaApp` → `ResearchRuntime` |
| Headless | `scripts/run_headless.py::main` | `_run()` → `ResearchRuntime.start()` |
| Python GUI 网关 | `src/gui_gateway/__main__.py::main` | `start_server()` → `GuiRequestHandler` / `WebSocketTransport` |
| GUI 服务层 | `src/athena/gui/service.py::GuiService` | 查询或控制 `ResearchRuntime` |
| React 前端 | `athena-gui/src/main.tsx` | `App.tsx` → hooks / shell components → `tauri-bridge.ts` |
| Tauri 壳 | `athena-gui/src-tauri/src/lib.rs` | Rust commands → Python bridge / WebSocket RPC |
| 研究运行时 | `src/athena/research/runtime/facade.py::ResearchRuntime` | 装配服务并创建 `Supervisor` |
| 阶段机 | `src/athena/research/supervisor/phases.py::PhaseMachine` | `PREPARE` → `SEARCH` → `VALIDATE` |
| 搜索循环 | `src/athena/research/supervisor/search_loop.py::SearchLoop` | Scheduler → Ideator / PlanLifecycle |
| 最终报告 | `src/athena/research/report.py::build_final_report` | ResearchTree + validation → Markdown |

如果只想理解一次完整运行，建议按以下顺序阅读：`scripts/run_headless.py` → `research/runtime/facade.py` → `research/supervisor/phases.py` → `research/runtime/phase_runner.py` → `research/supervisor/search_loop.py` → `research/supervisor/plan_lifecycle.py`。

## 7. 修改功能时应该从哪里开始

先从“谁拥有这项行为”定位，不要直接从界面或某个 Prompt 猜测：

| 要修改的功能 | 首先查看 | 通常还需联动 |
|---|---|---|
| 阶段顺序、终态、自动验证 | `research/supervisor/phases.py` | `research/runtime/phase_runner.py`、`research/supervisor/state.py`、阶段测试 |
| 搜索预算、并发和动作选择 | `supervisor/scheduling.py`、`search_loop.py` | `plan_lifecycle.py`、`plans.py` |
| 假设/实验/SOTA 数据结构 | `core/research_tree.py`、`research_models.py` | GUI 图模型、持久化兼容测试、报告 |
| 新增或修改业务 Agent | `src/athena/agents/<name>_agent.py` | `src/athena/agents/prompts/`、注册位置、Tool 权限和输出模型 |
| Agent 运行循环或 Tool 调用 | `core/agent/agent_runtime.py`、`core/tool.py` | `base_runner.py`、`tool_types.py`、工具单测 |
| PREPARE / VALIDATE 具体执行 | `research/runtime/phase_runner.py` | 对应 Agent、`research/evaluation/`、handoff 契约 |
| 初始 EDA、worker 并发与降级 | `research/prepare/eda.py`、`runtime/phase_runner.py` | EDA prompts、工作区产物、`test_eda_todo.py` |
| Ideator、动态 EDA、handoff | `research/turns/ideator.py` | `idea_generation/`、Data Agent、artifact 引用 |
| 任务理解和澄清 | `athena/gui/service.py::parse_intent` | `gui_gateway/human.py`、`handler.py`、`HumanRequestDialog.tsx`、`usePipeline.ts` |
| GUI RPC 方法 | `gui_gateway/handler.py::SUPPORTED_METHODS` | `GuiService`、`tauri-bridge.ts`、Rust commands、协议契约测试 |
| GUI 页面与交互 | `athena-gui/src/App.tsx`、`components/`、`hooks/` | `tauri-bridge.ts`、Vitest |
| 最终报告格式 | `research/report.py` | `GuiService.generate_report`、VALIDATE 测试 |
| 论文检索/语料 | `research/literature/survey/` | `cli.py` 的 `survey` 子命令、paper tools、Survey 测试 |
| 状态字段和断点续传 | `supervisor/state.py` | `runtime.py`、Recovery、旧状态迁移测试 |

推荐的修改顺序是：

1. 先读对应专题和现有测试，确认输入、输出与状态所有权。
2. 从领域模型或服务契约改起，再接流程编排和入口适配。
3. 最后改 GUI/TUI 展示；若修改 GUI RPC，同步检查 Python、TypeScript、Rust 三方方法名。
4. 为状态和持久化变化补迁移/恢复测试，不要只验证全新运行。
5. 运行最小相关测试，再运行后端与前端完整测试集。

常用验证命令：

```powershell
uv run pytest -q tests test/unit
Set-Location athena-gui
npm test
npm run build
```

## 8. 深入阅读

| 章节 | 主题 |
|---|---|
| [01](01-overall-architecture.md) | 整体架构、进程关系、GUI 协议与启动链路 |
| [02](02-app-server.md) | app server、Thread/Turn 协议与生命周期 |
| [03](03-agent-infra.md) | Agent 注册、运行循环、Provider、Session、Tool |
| [04](04-subagent-flow.md) | 子 Agent 的创建、等待、回收和生产用法 |
| [05](05-research-tree.md) | ResearchTree、假设、实验与 SOTA |
| [06](06-workflow.md) | PREPARE / SEARCH / VALIDATE 全流程 |
| [07](07-tree-search-and-tool.md) | Scheduler、Plan 生命周期、树搜索与 Tool 机制 |
| [08](08-idea-generation.md) | Ideator、证据门禁和动态 EDA |
| [09](09-memory-system.md) | 上下文压缩、rollout 和恢复 |
| [10](10-paper-research.md) | 论文检索、转换、索引与 Ideator 消费 |
| [11](11-evidence-and-todo.md) | 关键结论的源码索引与待确认项 |
| [12](12-eda-system.md) | PREPARE EDA、worker 调度、动态 EDA、产物与失败降级 |

文档中的实现结论应以源码和测试为最终依据。若文档、Prompt 与运行时代码不一致，优先核对 `ResearchRuntime`、`Supervisor` 及其契约测试，不要仅凭文件名推断真实调用链。

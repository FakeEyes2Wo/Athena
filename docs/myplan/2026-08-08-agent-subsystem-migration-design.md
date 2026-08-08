# Athena Agent 子系统迁移与重构设计

> 状态：方案 2 已确认，本轮实施阶段 A + B + C（SearchLoop 拆分）
> 日期：2026-08-08
> 上位设计：[agent-end-to-end-workflow-design.md](agent-end-to-end-workflow-design.md)、[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 范围：src/athena 内部（不改顶层目录）

## 1. 背景

代码库存在两条并行的 Agent 执行体系：

- **世界 A（live `main.py` 路径）**：`main.py -> ResearchRuntime -> workflows/{search,prepare,report,validate} + ideator`。跑通 Titanic 端到端（PREPARE→SEARCH→VALIDATE→REPORT），使用 pydantic-ai、codex/qoder 后端、git worktree 隔离执行。
- **世界 B（内核架构，已实现未接线）**：`ProjectRuntime -> core/agent_kernel(AgentKernel) -> agents/{supervisor,data,plot,reflection,ideator,code,report}Agent -> base_runner/orchestration -> core/agent`。设计文档已确认此架构为唯一目标（docs/myplan/ 六篇），但 `main.py` 仍未接入。

「两个同名 Agent」等结构问题：

| 概念 | 世界 A | 世界 B |
|---|---|---|
| 代码 Agent | `workflows/search/code_agent.py::CodeAgent`（真实实验执行） | `agents/code_agent.py::CodeAgent`（run_impl 存根） |
| 假设 Agent | `ideator/ideator.py::Ideator` | `agents/ideator_agent.py::IdeatorAgent` |
| 报告 Agent | `workflows/report/final_report.py::Reporter` | `agents/report_agent.py::ReportAgent` |
| 准备/验证 | `workflows/prepare/runtime.py`、`workflows/validate/ablation.py::Validator` | （无对应） |

大文件：`core/agent_kernel/kernel.py`（1612 行）、`core/agent/runtime.py`（397 行）。

## 2. 目标

只保留一套 Agent 执行体系：

```text
User / CLI -> ProjectRuntime -> root SupervisorAgent -> AgentKernel
           -> registered Agent instances -> deterministic services
```

本轮完成后：
1. `main.py` 装配 ProjectRuntime，端到端走内核路径；
2. 七类 Agent 的 `code`/`ideator`/`report` 使用真实实现（合并世界 A 能力）；
3. `SearchLoop` 类删除：编排迁入 SupervisorAgent，确定性逻辑留在 `experiment/`/`evaluation/` helper；
4. `kernel.py`、`runtime.py` 拆分为单职责模块；
5. 死代码与直接测被删模块的测试删除；
6. 测试集合（删除后）全绿，Titanic 冒烟通过。

明确不做（本轮）：App Server 改造、EvaluationPolicy 从报告正文推断的收尾、动态类型注册、双 Runtime 同步。

## 3. 目标 `src/athena` 结构

```text
src/athena/
├── agents/                    # 七类业务 Agent（唯一实现）
│   ├── __init__.py
│   ├── base_runner.py         # BaseAgent -> AgentKernel runner 适配（不变）
│   ├── orchestration.py       # RunToolProjector + 五编排工具（不变）
│   ├── supervisor.py          # root SupervisorAgent（+ SearchLoop 编排职责迁入）
│   ├── data_agent.py          # 真实 EDA/分析（已运行真实分析脚本，阶段 B 与 split 联动）
│   ├── plot_agent.py          # 通用绘图（不变）
│   ├── reflection_agent.py    # rubric/score/review（不变）
│   ├── ideator_agent.py       # 真实 Ideator.generate 经 run_impl 接入
│   ├── code_agent.py          # 真实实验 CodeAgent.execute（世界 A 迁入）
│   ├── report_agent.py        # 真实 Reporter.generate（世界 A 迁入）
│   └── production.py          # run_impl 构建器 + ProjectState（接真实能力）
├── core/
│   ├── agent/                 # Agent 运行时契约（拆分后）
│   │   ├── __init__.py
│   │   ├── models.py          # AgentContext/AgentOutcome/ToolCall/AgentConfig（不变）
│   │   ├── provider.py        # ResponsesProvider（不变）
│   │   ├── base.py            # BaseAgent 抽象类
│   │   ├── loop.py            # Agent 采样循环 + 工具分发
│   │   └── factories.py       # create_agent/agent_runner 工厂
│   ├── agent_kernel/          # 生命周期内核（拆分后）
│   │   ├── __init__.py
│   │   ├── kernel.py          # AgentKernel 编排/恢复/持久化调用（瘦身）
│   │   ├── scheduler.py       # AgentScheduler（从 kernel.py 拆出）
│   │   ├── graph.py           # AgentRegistry 树结构操作（从 kernel.py 拆出）
│   │   ├── commands.py        # KernelCommand + 命令载荷（从 kernel.py 拆出）
│   │   ├── registry.py        # AgentTypeRegistry（不变）
│   │   ├── session.py         # RunSession/AgentSession/resources（不变）
│   │   ├── store.py           # AgentGraphStore（不变）
│   │   ├── store_json.py      # JSON 持久化（不变）
│   │   ├── types.py           # 数据模型（不变）
│   │   ├── codec.py           # JsonCodec（不变）
│   │   └── control.py         # AgentControl/AgentHandle（不变）
│   └── …（contracts/research_tree/thread_models/tool/workspace 不变）
├── evaluation/                # 确定性评估
│   ├── policy.py / types.py / trusted.py / comparator.py（不变）
│   └── validator.py           # Validator（从 workflows/validate/ablation.py 迁入）
├── data/                      # 确定性 DatasetService
│   ├── domain.py / operations.py / tools.py（不变）
│   └── prepare.py             # split/profile/eval_spec 准备（从 workflows/prepare/runtime.py 迁入）
├── code/                      # 确定性执行引擎（不变，真实实验执行服务的宿主）
├── research/                  # ProjectRuntime（Composition Root）+ budget + ResearchTree
├── storage/                   # ArtifactStore/Bundle（不变）
├── experiment/                # 确定性 helper：ranker/comparator/supervisor(budget 决策)/ranking（不变）
├── ideator/                   # 真实 Ideator 实现（generate 被 agents 复用）
├── workflows/                 # ▸ 确定性逻辑并入上表后整个删除
└── app_server/                # 本轮不动；标注为后续改造为只调 ProjectRuntime
```

## 4. 阶段 A：结构就位（世界 A 仍跑，测试全绿）

### 4.1 拆分 `core/agent_kernel/kernel.py`（1612 行）

纯机械搬运，不改行为：

| 新模块 | 内容 | 来源行 |
|---|---|---|
| `scheduler.py` | `AgentScheduler`（就绪队列/活动上限/lease/park） | kernel.py:55-121 |
| `graph.py` | `AgentRegistry`（child_path/children/post_order/fences） | kernel.py:123-174 |
| `commands.py` | `KernelCommand` 与命令载荷 dataclass | kernel.py:176-185 |
| `kernel.py` | `AgentKernel` 剩余：serializer 队列、spawn/followup/wait/close 权威命令、`_execute_run`、恢复/重建 | 186-1612 |

拆分后 `kernel.py` 内 `AgentKernel` 仍集中实现命令状态机（同类状态机拆分到模块级收益低、回归风险高），只把可独立成类的 `AgentScheduler`/`AgentRegistry`/`KernelCommand` 移出。模块间通过 `kernel.py` 现有字段与 `AgentGraphStore` 交互，不在本轮引入新抽象。

验收：`test/unit/agent_kernel/*` 全绿；`from athena.core.agent_kernel.kernel import AgentKernel` 等既有导入路径不变。

### 4.2 拆分 `core/agent/runtime.py`（397 行）

| 新模块 | 内容 | 来源 |
|---|---|---|
| `base.py` | `BaseAgent` 抽象类 | runtime.py:40-56 |
| `loop.py` | `Agent` + `_sampling_loop` + `_dispatch_tool_call` + `_cancel_tool_tasks` | runtime.py:58-296 |
| `factories.py` | `agent_runner` + `create_agent` + `create_code_agent` | runtime.py:299-356 |
| `helpers.py` | `_to_str` / `_load_input` / `_has_system` | runtime.py:359-397 |
| `runtime.py` | 保留为 re-export 兼容层（或删除，见 4.4 清理） | — |

验收：`test/unit/agent_kernel/test_base_agent_runner.py` 等引用 `core.agent` 的测试全绿。

### 4.3 归并同名 Agent（双轨合一，行为不变）

原则：把世界 A 的真实实现搬为 `agents/` 对应类的真实路径；**世界 A 路径继续运行直到阶段 B 切换**，因此本轮只搬位置 + 消歧，不改变被调用方。

| 迁移 | 动作 |
|---|---|
| `workflows/search/code_agent.py` → `code/experiment_execution.py::ExperimentExecutionService` | 真实实验执行逻辑（`execute`/`execute_frozen`/`_generation_prompt` 等，含 git worktree 隔离、代码后端生成、可信评估、diff 评审、提交）迁为**确定性服务** `ExperimentExecutionService`。按 registered-agent-catalog §5，Git 提交、评估、SOTA 接受是确定性边界，不属 Agent。原 import 方（main.py、SearchLoop、Validator）更新为新路径。 |
| `agents/code_agent.py::CodeAgent` | 保留为 BaseAgent 包装：`run(ctx)` 经 `ProjectState` 解析实验输入，调用 `ExperimentExecutionService`，把 `CodegenResult` 写为 Artifact 并返回 `result_ref`。`production.py::code_run_impl` 即此桥接。 |
| `ideator/ideator.py::Ideator` → `agents/ideator_agent.py` | `production.py::ideator_run_impl` 接入真实 `Ideator.generate`；`agents/ideator_agent.py` 的真实路径可用。 |
| `workflows/report/final_report.py::Reporter` → `agents/report_agent.py` | `production.py::report_run_impl` 接入真实 `Reporter.generate(sota_id, tree)`。 |
| `workflows/prepare/runtime.py::prepare_workflow_data` → `data/prepare.py` | 确定性 split/profile/eval_spec 准备，作为 DatasetService。 |
| `workflows/validate/ablation.py::Validator` → `evaluation/validator.py` | 确定性验证服务，由 ProjectRuntime 阶段 B 调用。 |

**阶段 A 期间世界 A 保持运行**：上述迁移动文件时同步更新所有 import 方（main.py、SearchLoop、Validator、baseline 等）指向新路径，行为不变、测试全绿。阶段 C 删除 SearchLoop/Validator 的编排形态后，确定性服务继续存在。

### 4.4 死代码与过度抽象清理（清单）

- 删除根目录 `agent_tool_example.py`（演示脚本，未接入生产）。
- `agents/production.py` 中阶段 B 之前未接线的 `IdeatorInputs/CodeInputs/ReportInputs` dataclass 若阶段 B 接线则保留，否则删除；`ProjectState` Protocol 阶段 B 会用到，保留。
- `core/agent/runtime.py` 拆分后删除原文件（保留 re-export 需确认调用方，优先直接改 import 并删文件）。
- 其余以 grep 为准：本 repo 内无导入者的模块/类才可删。删除前 `git grep` 确认。

## 5. 阶段 B：切换 Composition Root

### 5.1 `ProjectRuntime` 补全真实能力

现 `ProjectRuntime` 只有最小确定性步骤（CONFIGURE/PREPARE-EDA/SEARCH-记录/REPORT-骨架）。阶段 B 增加：

- `prepare_baseline(...)`：调用 `data/prepare.py` 冻结 split + `evaluation/validator.py` 无关的 baseline 实验（用 `agents/code_agent.py` 真实实现）→ ResearchTree 提交 baseline；
- `run_search(...)`：Supervisor 编排 IdeatorAgent→CodeAgent→comparator→ResearchTree SOTA，预算与决策用 `experiment/` helper；
- `run_validate(...)`：调用 `evaluation/validator.py`（ablation + 唯一 frozen final-test）；
- `run_report(...)`：`agents/report_agent.py` 真实 Reporter 从 ResearchTree 证据生成报告。

阶段推进与状态投影保持现有 `projected_phase`/`project_status` 方式，权威事实只在提交时耐久化。

### 5.2 `main.py` 重写

- 不再装配 `ResearchRuntime`/`ResearchWorkflowDependencies`；
- 装配 `ProjectRuntime`，CLI 参数映射到项目方法（configure → prepare → search → validate → report），复用现有参数校验与 summary 输出；
- `run_summary.json` 字段保持不变（phase/sota_id/tree_path/report_ref/backend/execution/budget）。

### 5.3 `ResearchRuntime` 降级

- `research/runtime.py` 保留为协议翻译层：`dispatch` 方法翻译为 `ProjectRuntime` 调用，不再创建 Task、保存 phase、管理 pause、运行 workflow coroutine；`_run_task`/`_run_search` 删除。
- `gui_gateway` 仍可调用 `ResearchRuntime`（翻译层），本轮不改 gui_gateway。

## 6. 阶段 C（本轮只做 SearchLoop 拆分）

### 6.1 SearchLoop 拆分

- 确定性逻辑**保留为 helper**（留在 `experiment/`、`evaluation/`）：`HypothesisRanker.select`、`ProximityGraph.update/add`、`Comparator.compare`、`Supervisor.decide`、`BudgetSnapshot.consume/is_exhausted`。
- 编排职责**迁入 `SupervisorAgent`**：按阶段事实 spawn IdeatorAgent（生成假设）→ 选假设 → spawn CodeAgent（execute）→ comparator → decide → 更新 ResearchTree → 预算内循环。
- **删除** `workflows/search/search_loop.py::SearchLoop` 类；`workflows/search/idea_generation.py::PaperSearch/generate_hypotheses` 保留为确定性 helper（供 IdeatorAgent 真实实现复用）。

### 6.2 App Server（本轮不做，标注后续）

`app_server/` 仍是独立 Thread/Turn 运行时，持有第二套 Agent 生命周期。**本轮不改造**，在 README/设计文档标注为后续工作：改为只调 `ProjectRuntime.open/message/human_reply/pause/resume/stop/status`，消除第二套生命周期后满足端到端验收 §10。

## 7. 测试策略（T3 + 删除）

- **保留并适配新路径**：evaluation、code 执行引擎、data、research_tree、agent_kernel、prepare split 等服务测试。
- **删除直接测被删模块的测试**（不保留 skip 死代码）：
  - `tests/test_search_workflow.py`（测 SearchLoop）
  - `tests/test_main_workflow.py` 中测 ResearchRuntime 内部 / workflows 编排的部分
  - `tests/test_prepare_workflow.py` 中测 `workflows/prepare` 内部的部分（split 逻辑迁 `data/prepare.py` 后改写对应测试）
  - `tests/test_e2e_ai4ml.py` 中依赖 ResearchRuntime 编排的部分
  - 其他以 grep 为准：import 被删模块的测试删除或改写。
- **新增**：ProjectRuntime 端到端测试（确定性骨架 + Titanic 冒烟）。

## 8. 验收

1. `uv run pytest -q tests test/unit`（删除后集合）全绿；
2. Titanic：`uv run python src/main.py --data examples/titanic/train.csv --target Survived --model openai:deepseek-chat --backend codex --output-dir .athena/titanic-run --max-experiments 1 --max-no-improve 1` 端到端走内核路径，产出报告与 `run_summary.json`；
3. 源码中不存在第二套 Agent lifecycle owner（`ResearchRuntime._run_task`、`SearchLoop`、旧 `AgentTask` 删除）；
4. 七类 factory 均为真实实现（无 run_impl 存根缺省路径残留，或残留仅限未接线且未使用）；
5. `kernel.py` 拆分后 `test/unit/agent_kernel/*` 全绿；`runtime.py` 拆分后相关测试全绿。

## 9. 风险与不变量

- **阶段 B 是主风险**：ProjectRuntime 补全真实能力 + main.py 切换是行为变更，以 Titanic 冒烟为硬验收；若后端/凭据不可用，用确定性骨架路径冒烟。
- **不变量**：AgentKernel 契约（AgentSpec/AgentRunner/AgentCodec）、AgentMessage/AgentOutcome 最小合同、ResearchTree/ArtifactStore 所有权不变。
- **顺序**：阶段 A（测试全绿）→ 阶段 C 的 SearchLoop 拆分（测试全绿）→ 阶段 B 切换（以冒烟验收）。App Server 留待后续。

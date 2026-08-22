# PR：PREPARE 阶段 dataclean agent + PR7 HF/MCP 工具移植 + 全链路 E2E

> **分支**：`feat/prepare-dataclean-pr7-tools` → **目标**：`main`
>
> **状态**：待合并（前后端均已实测通过）

---

## 一、背景与目标

PREPARE 阶段目前由 `evaluator → EDA → baseline_ideator → prepare` 组成。本次 PR 补齐三个能力：

1. **新增 `dataclean` agent**：在 evaluator 之后、EDA 之前，负责分析已下载的数据并**按数据集逐例决策**清洗方法（可能编写并执行代码），产出 `DATACLEAN_HANDOFF.md` 交给 EDA。
2. **移植 PR7 分支的 HuggingFace / MCP 工具**：让 PREPARE 阶段的 dataclean、baseline_ideator、evaluator 三个 agent 能调用 HF 数据集/模型搜索下载与 MCP 工具（`pr7_review/src/athena/tools` → `src/athena/tools`）。
3. **全 PREPARE 阶段 E2E 测试**：用 agent-aware 假客户端模拟前端 prompt 走完整个 PREPARE 链路，含 dataclean 一个修复轮。

## 二、变更总览（相对 `main`）

**34 个文件，+4711 / -7 行**，10 个提交：

| 提交 | 说明 |
|---|---|
| `e21ead9` | 实现计划文档 |
| `5ffc907` | 依赖：`huggingface_hub` / `mcp` / `httpx` |
| `948e22b` | 移植 HF 工具（`hf_dataset` / `hf_model`）+ 裁剪 |
| `f538dec` | 移植 MCP 工具层（`mcp/*`，无 server 配置，dormant） |
| `ef6e49d` | `ResearchRuntime` 装配 HF/MCP 工具到各 agent 的 `extra_tools` |
| `47c3dbb` | dataclean agent 模块（`prepare_dataclean_agent.py`）+ 数据无关 prompt |
| `3ea2de3` | `run_dataclean_plan` 修复循环（PlanDecision 输出，iterate 到 verified）+ kaggle 工具接线 |
| `b153ec1` | `phase_runner` 接入 dataclean 步骤（evaluator 后、EDA 前） |
| `59ba686` | 全 PREPARE E2E 测试 + **修复两个 async publish 生产 bug** |
| `98c38ee` | 计划文档补充 Task 8 实际落地差异 |

## 三、改动详解

### 1. dataclean agent（任务 1）

- **模块**：`src/athena/agents/prepare_dataclean_agent.py`（与 evaluator 命名一致），工作目录 `workspaces/dataclean`。
- **数据无关的完成判定**（A1/A2 决策）：gate 只认 `DATACLEAN_HANDOFF.md` 非空 + `decision: submit`，**不校验任何固定产物**——对不同数据集由 agent 自行决定要不要写清洗代码、要不要清洗。这也保证断点续传时可复用。
- **修复循环**：沿用 evaluator 的 `run_*_plan` 模式（PlanDecision 输出，未过 gate 打回迭代，预算耗尽放弃）。
- **接入顺序**：`run_prepare_phase` 步骤 1b —— evaluator 冻结之后、EDA 之前；EDA 的 handoff 内容会追加 dataclean 的清洗说明；`kaggle/wiring.py` 给 dataclean 注入 `get_competition` / `download_data`。

### 2. PR7 HF / MCP 工具移植（任务 2–4）

- **HF 工具**（`src/athena/tools/hf_dataset.py`、`hf_model.py`）：数据集/模型搜索 + 下载，经 `agent_tools()` / `baseline_ideator_tools()` 装配到 **dataclean、baseline_ideator、evaluator** 三个 agent 的 `extra_tools`。
- **MCP 工具**（`src/athena/tools/mcp/`）：`adapter` / `client` / `config` / `search`。按约定只移植**工具层**，不配置 server——仓库根 `mcp_servers.json` 是空列表，MCP 工具 dormant（不加载），HF 工具 active。仅 baseline_ideator 拿到 HF+MCP（SEARCH ideator 不动）。
- 移植时对 PR7 代码做了裁剪（去掉与 PREPARE 无关的耦合），保持轻量、工程化。

### 3. 全 PREPARE E2E 测试（任务 8）

`test/integration/research/test_prepare_phase_e2e.py`：

- agent-aware 假客户端按 system prompt 标记分发逐 turn 剧本；编排 / 文件 IO / git / 评估脚本执行全真。
- Kaggle 关闭（`configure_kaggle`），用本地合成 MAGFiLO 形状数据。
- 断言：evaluator / dataclean / EDA / baseline / prepare 全链路产物就位，`experiment.eval.primary == 1.0`，**dataclean 恰好提交两次**（一个修复轮）。

### 4. 顺带修复：两个 async publish 生产 bug

E2E 运行暴露了既有真实 bug——`phase_runner._run_handoff_agent` 与 `eda_todo._run_one_todo` 的 publish 闭包调用异步事件转发但**未 await**（返回 None），`forward_run_events` 里 `await publish(...)` 在真实 `_events_bus` 下抛 `TypeError`。已改为 `async def publish(...) -> None` 并 `await`。此 bug 在真实 PREPARE 只要 `_run_handoff_agent` 跑且存在 `_events_bus` 就会触发。

## 四、测试结果

| 测试 | 结果 |
|---|---|
| `test/unit/tools`（HF + MCP 移植） | PASS |
| `test/unit/research/test_runtime_pr7_tools.py` | PASS |
| `test/unit/agent/test_prepare_dataclean_agent.py` | PASS |
| `test/integration/research/test_dataclean_agent_contract.py` | PASS（含 gate 数据无关性参数化） |
| `test/integration/research/test_prepare_phase_dataclean_order.py` | PASS（evaluator 后、EDA 前） |
| `test/integration/research/test_prepare_phase_e2e.py`（slow） | PASS（完整 PREPARE 链路） |
| `pytest -m "not slow"` 全量 | **29 failed, 1868 passed** —— 29 个失败全部为既有问题（见下），**本次改动零回归** |

### 已知既有失败（与本 PR 无关，均已基线核验）

- `test_fork.py`（14）、`test_ideator_wiring.py`（4）、`test_experiment_timeout_wiring.py`（1）、`test_all_phases_use_one_authority`（1）——既有 20 个。
- `test_search_recovery.py`（1）、`test_fork_runtime.py`（3）、`tests/test_gui_gateway_e2e.py`（1）、`tests/test_gui_gateway_transport.py`（4）——本次全量新出现 9 个，**已用 `git stash` 干净基线复跑确认与本次改动无关**（GUI gateway 是测试 fixture 签名问题 `factory() takes 1 positional argument but 2 were given`）。

## 五、验证方式

```bash
# 针对本 PR 的定向回归
uv run pytest test/unit/tools test/unit/research/test_runtime_pr7_tools.py \
    test/unit/agent/test_prepare_dataclean_agent.py \
    test/integration/research/test_dataclean_agent_contract.py \
    test/integration/research/test_prepare_phase_dataclean_order.py -v

# E2E 完整链路
uv run pytest test/integration/research/test_prepare_phase_e2e.py -v -m slow
```

## 六、说明 / 范围外

- `pr7_review/` 是移植参考分支，**不进 git**（保持 untracked）。
- MCP 工具 dormant（无 server 配置），如需启用由后续 PR 在 `mcp_servers.json` 配置。
- `runtime.py` 中 GUI settings 快照的 `handoff_sources` 一行注释为本地调试改动，**未包含在本 PR**。
- 设计文档见 `docs/superpowers/specs/2026-08-22-prepare-dataclean-pr7-tools-design.md`，实现计划见 `docs/superpowers/plans/2026-08-22-prepare-dataclean-pr7-tools.md`。

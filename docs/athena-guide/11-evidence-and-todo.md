# 11 证据索引与待确认项

## 11.1 整体架构

| 结论 | 证据 |
|---|---|
| Python 为生产主入口 | `pyproject.toml`、`athena-rust/README.md:5-6` |
| 默认端口 17601 | `src/gui_gateway/__main__.py:42-55` |
| WebSocket DTO | `src/gui_gateway/transport.py:1-15` |
| 前端 WS | `athena-gui/src/lib/ws-backend.ts:35-144` |
| 组合根 | `src/athena/research/runtime/facade.py:157` |

## 11.2 app_server

| 结论 | 证据 |
|---|---|
| DTO 约束 | `src/athena/app_server/protocol.py:13-16` |
| 错误码 | `src/athena/app_server/protocol.py:19-30` |
| 方法名 | `src/athena/app_server/protocol.py:63-82` |
| 传输 | `src/athena/app_server/transport.py:18-34` |
| 状态机 | `src/athena/app_server/server.py:30-38` |
| 执行 | `src/athena/app_server/execution.py:23-73` |
| ThreadManager | `src/athena/app_server/thread_manager.py:21-288` |
| ThreadRuntime | `src/athena/app_server/thread_runtime.py:80-633` |
| Lifecycle | `src/athena/app_server/lifecycle.py:117-181` |

## 11.3 Agent

| 结论 | 证据 |
|---|---|
| Registry | `src/athena/core/agent/registry.py:20-50` |
| AgentRuntime | `src/athena/core/agent/agent_runtime.py:107-144` |
| Agent 循环 | `src/athena/core/agent/runtime.py:74-200` |
| Provider | `src/athena/core/agent/provider.py:211-339` |
| Session | `src/athena/core/agent/session.py:18-82` |
| Runner | `src/athena/agents/base_runner.py:66-142` |

## 11.4 Subagent

| 结论 | 证据 |
|---|---|
| Python | `src/athena/core/agent/agent_runtime.py:211-246`、`555-586` |
| EDA worker | `src/athena/research/prepare/eda.py:50-99` |
| Ideator lane | `src/athena/research/turns/ideator.py:538-644` |
| 编排工具 | `src/athena/agents/orchestration.py:69-215` |
| Rust | `athena-rust/crates/athena-agent/src/subagent.rs:33-229` |

## 11.5 ResearchTree / SEARCH / Tool

| 结论 | 证据 |
|---|---|
| Hypothesis | `src/athena/core/research_models.py:18-42` |
| Experiment | `src/athena/core/research_tree.py:38-73` |
| 状态机 | `src/athena/core/research_tree.py:76-89` |
| Scheduler | `src/athena/research/supervisor/scheduling.py:20-169` |
| Plan | `src/athena/research/supervisor/plan_lifecycle.py:73-163` |
| Elo | `src/athena/research/supervisor/scheduling.py:32-57` |
| Selector | `src/athena/research/supervisor/scheduling.py:92-169` |
| Tool | `src/athena/core/tool.py`、`tool_types.py` |
| 工具分发 | `src/athena/core/agent/runtime.py:411-450` |

## 11.6 Idea Generation / Memory

| 结论 | 证据 |
|---|---|
| 门禁入口 | `src/athena/research/idea_generation/gate.py:156` |
| pre_gate | `src/athena/research/idea_generation/gatekeeper.py:117-148` |
| light_hard_gate | `src/athena/research/idea_generation/gatekeeper.py:151-246` |
| 动态 EDA | `src/athena/research/turns/ideator.py:319-365` |
| ContextManager | `src/athena/memory/context_manager.py:16-118` |
| Compactor | `src/athena/memory/compaction.py:26-138` |
| Rollout | `src/athena/memory/rollout.py:26-199` |

## 11.7 论文系统

| 结论 | 证据 |
|---|---|
| Survey | `src/athena/research/literature/survey/wiring.py`、`pipeline.py` |
| PaperScout | `src/athena/research/literature/paper_scout/` |
| paper_source | `src/athena/research/literature/paper_source/` |
| paper_markdown | `src/athena/research/literature/paper_markdown/` |
| paper_rag | `src/athena/research/literature/paper_rag/` |
| Ideator 消费 | `src/athena/research/runtime/facade.py:596-707`、`turns/ideator.py:689-716` |

## 11.8 EDA

| 结论 | 证据 |
|---|---|
| EDA 工作区与相对路径 | `src/athena/research/runtime/phase_runner.py:176-188` |
| PREPARE EDA 编排与 fallback | `src/athena/research/prepare/orchestrator.py`、`src/athena/research/prepare/eda.py` |
| todo 阶段并发、重试与 checkbox | `src/athena/research/prepare/eda.py` |
| EDA orchestrator / worker 契约 | `src/athena/agents/prompts/prepare_eda_agent.md`、`src/athena/agents/prompts/eda_worker_agent.md` |
| 动态 EDA | `src/athena/research/turns/ideator.py:217-365` |
| 动态写入边界 | `src/athena/agents/prompts/data_agent.md` |
| 分叉继承 EDA 工作区 | `src/athena/research/fork.py::_carry_eda_workspace` |

详细说明见 [12 EDA 系统设计](12-eda-system.md)。

## 11.9 待确认

1. 不存在 `ToolExecutor` 类。
2. `idea_generation` 中不存在 `workflow.py`、`ranking.py`、`candidate_generation.py`、`hypothesis_selector.py`。
3. `hard_gate` 不存在。
4. Pairwise Elo 不存在。
5. Python 生产 Supervisor 是否暴露 spawn/wait 编排工具未知。
6. Rust 结构化输出未实现。
7. `athena-gui/src-tauri/src` 未完整阅读。
8. `external/` 子模块可能未初始化。
9. Rust memory 是否接入 Python 运行时未知。

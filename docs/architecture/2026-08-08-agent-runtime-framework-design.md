# Agent 运行时框架（Codex-CLI 风格）

> **Superseded (2026-08-08)**：被 [`2026-08-08-thread-agent-unification-design.md`](2026-08-08-thread-agent-unification-design.md) 取代 ——
> 方向反转，以 app_server Thread 模型为统一底座，本文的 AgentKernel 设计退役。
>
> 2026-08-08 · 上位：`agent-end-to-end-workflow-design.md`（历史）

## 现状

`core/agent/` 核心未变：`Agent(model, tools, system_prompt, config)`（ReAct 循环返回 `result://{turn_id}`），`agent_runner`/`create_agent`/`create_code_agent` 均在；`provider.stream` 已支持 `response_format`（Task 1 已提交）。`agents/` 中 `IdeatorAgent` 为 `run_impl` 单一注入（`production.ideator_run_impl`），其余 agent 为确定性 `BaseAgent`；`register_defaults(self)` 尚无 `model` 参数；evaluation 包已整体删除。

## 目标

引入 LLM 驱动 `Agent` 作通用执行引擎，支持可选结构化输出。
**首版仅框架 + 映射文档，不转换任何 agent；`data`/`code` 不变；supervisor 保留 Manager（spawn/send/wait）。**

## 改动

| 文件 | 改动 |
|---|---|
| `core/agent/runtime.py` | `Agent(..., output_type=None, artifacts=None)`；有值→累计 JSON→`model_validate_json`（失败回喂≤3）→写 `artifacts` 返回真实 ref；无值→现状自由文本 |
| `core/agent/provider.py` | `stream(..., output_type=None)`；有值→`kw["response_format"]`（✅ 已完成） |
| `agents/agent_factory.py`（新） | `create_agent_for(agent_type, *, model, tools, system_prompt, output_type=None, artifacts=None)` |
| `research/project_runtime.py` | `register_defaults(*, model=None)`：模块级 `LLM_AGENT_MAPPING={}`（首版空）；有 model 且映射非空才注册 LLM Agent，否则回退骨架 |
| `agents/tools/`（新） | `write_script` / `run_script`(沙箱) / `commit_result` |
| kernel 集成 | 复用 `BaseAgentRunner`；结构化结果由 `artifacts` 注入落盘 |

## 契约

- `output_type` 传入才走 `response_format`，否则自由文本。
- `data`/`code` 不变；supervisor Manager 不变；无 model 回退骨架。

## 映射

| agent_type | 说明 | 首版 |
|---|---|---|
| supervisor | LLM Agent + spawn/send/wait（Manager） | 否 |
| data | 脚本式 DataAgent（不变） | 否(不变) |
| code | 骨架（不变） | 否(不变) |
| plot | LLM Agent + run_script/commit_result → 图片产物 | 否 |
| reflection | LLM Agent + commit_result → ReviewBundle | 否 |
| report | LLM Agent + commit_result → ReportBundle | 否 |
| ideator | **run_impl 注入**（`production.ideator_run_impl`，自有引擎）——不用 Agent 框架 | 否 |

## 测试

- provider：`response_format` 有/无。
- Agent：结构化（回喂重试）/ 自由文本。
- kernel：一个 turn → `result_ref` 为持久化 JSON；无 model 回退骨架。

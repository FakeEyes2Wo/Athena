# Agent 运行时框架（Codex-CLI 风格）

> 2026-08-08 · 上位：`agent-end-to-end-workflow-design.md`

## 目标

业务 Agent 目前都是确定性 `BaseAgent`。引入 LLM 驱动 `Agent`（`core/agent/runtime.py`，Codex-CLI 式工具循环）作通用引擎，支持可选结构化输出。
**首版仅框架 + 映射文档，不转换任何 agent；`data`/`code` 不变；supervisor 保留 Manager（spawn/send/wait）。**

## 改动

| 文件 | 改动 |
|---|---|
| `core/agent/runtime.py` | `Agent(..., output_type=None)`；有值→累计 JSON→`model_validate_json`（失败回喂≤3）→经 `commit_result` 落盘；无值→现状自由文本 |
| `core/agent/provider.py` | `stream(..., output_type=None)`；有值→`kw["response_format"]={"type":"json_schema","json_schema":{name,schema}}` |
| `agents/agent_factory.py`（新） | `create_agent_for(agent_type, *, model, tools, system_prompt, output_type=None)` |
| `research/project_runtime.py` | `register_defaults(model=None)`：有 model→按映射注册 LLM Agent；无→回退骨架 |
| `agents/tools/`（新） | `write_script` / `run_script`(沙箱) / `commit_result` |
| kernel 集成 | 复用 `BaseAgentRunner`；结果持久化走 `commit_result`，runner 不处理 |

## 契约

- `output_type` 传入才走 `response_format`，否则自由文本。
- `data`/`code` 不变；supervisor Manager 不变；无 model 回退骨架。

## 映射

| agent_type | prompt | tools | output_type | 首版 |
|---|---|---|---|---|
| supervisor | supervisor.md | spawn/send/wait | — | 否 |
| data | data_agent.md | — | — | 否(不变) |
| code | code_agent.md | — | — | 否(不变) |
| plot | plot_agent.md | run_script/commit_result | 图片产物 | 否 |
| reflection | reflection.md(新建) | commit_result | ReviewBundle | 否 |
| report | report.md(新建) | commit_result | ReportBundle | 否 |
| ideator | ideator.md(新建) | — | DebateResult | 否(自有引擎) |

## 测试

- provider：`response_format` 有/无。
- Agent：结构化（回喂重试）/ 自由文本。
- kernel：一个 turn → `result_ref` 为持久化 JSON；无 model 回退骨架。

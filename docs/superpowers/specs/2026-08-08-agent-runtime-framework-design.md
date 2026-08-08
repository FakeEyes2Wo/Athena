# Agent 运行时框架设计（Codex-CLI 风格）

- 日期：2026-08-08
- 状态：草案
- 上位：`docs/myplan/agent-end-to-end-workflow-design.md`

## 背景与目标

AgentKernel 现有业务 Agent 均为确定性 `BaseAgent` 骨架。本次引入 LLM 驱动的
`Agent`（`core/agent/runtime.py`，Codex-CLI 式 think→act→observe 工具循环）作为
通用执行引擎，参考 Codex CLI：工具即能力、可选结构化输出（`output_type`）。

**首版范围：仅框架 + 映射文档。** 不实际转换任何业务 agent；`data`（脚本式
DataAgent）与 `code`（骨架）保持现状；supervisor 保留 Manager 编排
（spawn/send/wait）。

## 架构

```
AgentKernel（调度层：session / mailbox / wait / spawn）
  └─ BaseAgentRunner（复用）── 适配 AgentSpec.runner
        └─ Agent（LLM 循环）= instructions + tools + model + output_type?
              ├─ 业务工具：write_script / run_script / commit_result
              └─ 编排工具：spawn / send / wait_for（supervisor）
```

## 改动清单

### 1. `core/agent/runtime.py` — Agent 可选结构化输出

- 签名：`Agent(model, tools, system_prompt, config, *, output_type: type[BaseModel] | None = None)`
- `run()` 终止逻辑：
  - `output_type` 有值：累计流式文本为 JSON → `output_type.model_validate_json(text)`；
    校验失败把错误回喂模型重试（≤3 次）；成功产出结构化结果。
  - `output_type` 为 `None`：维持现状（输出纯文本即终止）。
- 结构化结果经 `commit_result` 工具写入 ArtifactStore，`AgentOutcome.result_ref` 为真实 ref。

### 2. `core/agent/provider.py` — stream 支持 response_format

- 签名：`stream(config, tools, messages, cancel, *, output_type=None)`
- `output_type` 非空时追加：

  ```python
  kw["response_format"] = {
      "type": "json_schema",
      "json_schema": {"name": t.__name__, "schema": t.model_json_schema()},
  }
  ```

### 3. `agents/agent_factory.py`（新）— LLM Agent 构造

- `create_agent_for(agent_type, *, model, tools, system_prompt, output_type=None) -> Agent`
- `ProjectRuntime.register_defaults(model=None, client=None)`：model 非空时按映射
  注册 LLM Agent；为空时回退确定性 `BaseAgent` 骨架。

### 4. `agents/tools/`（新）— 业务工具

- `write_script` / `run_script`（复用 `LocalExperimentRuntime` 沙箱）/ `commit_result`。
- supervisor 沿用现有 `spawn` / `send` / `wait_for`（不变）。

### 5. kernel 集成

- 复用 `BaseAgentRunner`（`Agent` 是 `BaseAgent`），无需新 runner。
- 结果持久化由 `commit_result` 工具完成，runner 不做额外处理。

## 契约

- `output_type` 可选：**传入才走 `response_format` 结构化返回，否则自由文本。**
- `data` / `code` 不变；supervisor Manager 编排不变。
- 未配置 model 时回退确定性骨架。

## 映射（附录）

| agent_type | prompt | tools | output_type | 首版转换 |
|---|---|---|---|---|
| supervisor | supervisor.md | spawn / send / wait_for | — | 否 |
| data | data_agent.md | —（脚本式） | — | 否（不变） |
| code | code_agent.md | —（骨架） | — | 否（不变） |
| plot | plot_agent.md | run_script / commit_result | 图片产物 | 否 |
| reflection | reflection.md（新建） | commit_result | ReviewBundle | 否 |
| report | report.md（新建） | commit_result | ReportBundle | 否 |
| ideator | ideator.md（新建） | — | DebateResult | 否（自有辩论引擎） |

## 测试

- provider：`response_format` 有 / 无 output_type。
- Agent：结构化循环（校验失败回喂重试）、自由文本循环。
- kernel：LLM Agent 跑通一个 turn → `result_ref` 为持久化 JSON；无 model 时回退确定性骨架。

# M1 @athena/agent 智能体框架 Implementation Plan

> 状态：已完成（vitest 123 全绿、`tsc --noEmit` 无错误）。本计划记录实施任务拆分与对拍结论。

**Goal:** 在 `athena_ts/packages/athena-agent/` 用 TypeScript 移植 Python `src/athena/core/agent/*`（除 `agent_runtime.py`）、`src/athena/memory/*`、`utils/single_turn_chat.py`、`core/tool.py` + `tool_types.py`，zod 复刻消息模型，移植对应 pytest 为 vitest 对拍。

**Architecture:** 模型层（messages/tool-types/agent types）为纯 zod 或纯数据类，无 cordis；`tool()` 工厂取代 `@tool` 装饰器；`Agent`/`ResponsesProvider`/`ContextManager` 等为可直接构造的普通类（测试与 Python 一致）；`agent_runtime.ts` 延后 M4。

---

### Task 1: 脚手架 @athena/agent 包

- [x] 建 `packages/athena-agent/package.json`（`@athena/agent`，deps `@athena/core` + `zod`）与 `tsconfig.json`
- [x] 根 `package.json` build 脚本改为 `tsc -p core && tsc -p agent`；`npm install --legacy-peer-deps` 链接工作区
- [x] 构建 `@athena/core` 产出 `dist`（agent 依赖其构建产物）

### Task 2: 消息模型 + 取消原语 + 工具层

- [x] `src/messages.ts`：ModelRequest/ModelResponse/parts + `ModelMessagesTypeAdapter`
- [x] `src/cancel.ts`：`CancellationToken`/`CancelledError`
- [x] `src/tool-types.ts`：EmitEvent/AskUser/ToolSpec/ToolResult/ToolContext/truncateText/常量
- [x] `src/tool.ts`：`BaseTool`/`ToolRegistry`/`tool()`
- [x] `test/tool.test.ts`（对拍 test_tool.py 行为子集）

### Task 3: memory 层

- [x] `src/memory/context-manager.ts` + `test/memory/context-manager.test.ts`
- [x] `src/memory/compaction.ts` + `test/memory/compaction.test.ts`
- [x] `src/memory/rollout.ts` + `test/memory/rollout.test.ts`
- [x] `src/memory/index.ts`

### Task 4: agent 类型/配置/注册/会话

- [x] `src/agent/types.ts` + `test/agent/types.test.ts`
- [x] `src/agent/models.ts`
- [x] `src/agent/settings.ts` + `test/agent/settings.test.ts`
- [x] `src/agent/registry.ts` + `test/agent/registry.test.ts`
- [x] `src/agent/session.ts` + `test/agent/session.test.ts`

### Task 5: provider + runtime + 工具 + 单轮

- [x] `src/agent/provider.ts`（`_DeepSeekTextFilter`/`StreamEvent`/`ResponsesProvider`/三 provider/`create_provider`/`_to_api`）+ `test/agent/provider.test.ts`
- [x] `src/agent/tools/user-input.ts`（`RequestUserInputTool`）
- [x] `src/agent/runtime.ts`（`BaseAgent`/`Agent`/采样循环/`agent_runner`/`create_agent`/`create_code_agent`）
- [x] `test/agent/agent.test.ts`（对拍 test_agent.py 行为子集）
- [x] `src/single-turn-chat.ts` + `test/single-turn-chat.test.ts`

### Task 6: 公开面 + 全量验证

- [x] `src/index.ts`（M1 公开面）
- [x] `test/public-api.test.ts`（对拍 test_core_public_api.py）
- [x] 全量验证：`tsc --noEmit`（core + agent）无错误；`npm test` 24 文件 / 202 测试全绿

---

## 实施增补（相对 spec §4 的落地记录）

- **`tool()` 工厂**：`ToolFn = (input: Record<string, unknown>) => unknown | Promise<unknown>`；`ToolOptions` 含 name/description/inputSchema/concurrencySafe/maxResultChars。
- **`ToolCall.arguments` → `args`**：TS 类体 strict 模式禁止 `arguments` 字段名，故 `ToolCall.args` 替代 Python 的 `arguments`。
- **`_to_api` 的 `create` kw 拷贝**：Python `create(**kw)` 每次解包生成新 dict；TS `create({...kw})` 显式浅拷贝，避免 retry 时 `delete kw["response_format"]` 污染首轮 `calls[0]`。
- **`getClient()`**：无 key 抛 `Error("Missing LLM API key: ...")`；有 key 返回占位 client（真实 SDK 延后 M2）。
- **结构化输出**：`StructuredOutputType` 聚合 `name/modelJsonSchema/parseJson/toJson`；provider 用前两者注入 schema，runtime 用后两者校验/序列化。
- **测试未被 `tsc` 覆盖**：`tsconfig.json` `include: ["src"]`（与 M0 一致），测试由 vitest（esbuild）转译；类型错误靠 vitest 运行时捕获。

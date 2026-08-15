# athena-ts 重写设计（M1：@athena/agent 智能体框架）

日期：2026-08-14
状态：已实施（随实现记录）

## 1. 背景与目标

M0（`@athena/core`）已完成工程底座（zod 模型层 + workspace/git/artifact 基础设施 + cordis 组合根）。M1 在其上新增 `@athena/agent` 包，把 Python 的**智能体框架核心**移植到 TypeScript/Node：

- `src/athena/core/agent/*`（除 `agent_runtime.py`，见 §6 延后）
- `src/athena/memory/*`（context_manager / compaction / rollout）
- `src/athena/utils/single_turn_chat.py`
- `src/athena/core/tool.py` + `src/athena/core/tool_types.py`

目标仍是**摆脱 Python 运行时**：编排、TUI、agent 框架跑在 Node 上；SEARCH/VALIDATE 的 LLM 数据科学脚本仍以 Python 子进程执行（M2 起）。

## 2. 技术栈决策（沿用 M0 并新增）

| 关注点 | 决策 |
|---|---|
| 运行时/工具 | Node ≥22 · npm workspaces · TS 5.x strict + NodeNext(ESM，`module: ESNext` + `moduleResolution: Bundler`，同 M0 偏差) |
| 模型校验 | zod v4；消息模型为 discriminated union（`kind`/`part_kind`） |
| 消息模型 | `pydantic_ai.messages` 无 TS 等价 → 自建 `src/messages.ts`（见 §4） |
| 取消原语 | `asyncio.Event`/`asyncio.CancelledError` → 自建 `src/cancel.ts`（`CancellationToken`/`CancelledError`） |
| 工具装饰器 | Python `@tool` 依赖运行时内省 → TS `tool(fn, opts)` 工厂（显式 name/description/inputSchema） |
| LLM client | `openai` SDK 无 M1 测试覆盖 → 最小 `ChatClient` 结构接口；真实 SDK 接线延后 M2 |
| 测试 | vitest；逐一对拍 pytest |

## 3. 包结构与模块映射

```
athena_ts/packages/athena-agent/
  package.json                 # name @athena/agent, deps: @athena/core + zod
  tsconfig.json
  src/
    messages.ts                # pydantic_ai ModelMessage 移植
    cancel.ts                  # CancellationToken / CancelledError
    tool-types.ts              # EmitEvent/AskUser/ToolSpec/ToolResult/ToolContext/truncateText
    tool.ts                    # BaseTool/ToolRegistry/tool()
    memory/
      context-manager.ts       # ContextManager
      compaction.ts            # Compactor/Compaction
      rollout.ts               # RolloutRecorder/resumeContext
      index.ts
    agent/
      types.ts                 # AgentStatus/RunStatus/ErrorCode/AgentError 层级/JsonCodec/AgentSpec/...
      models.ts                # AgentConfig/AgentOutcome/StepOutcome/ToolCall/AgentContext
      settings.ts              # apiKey/baseUrl/modelName/proModelName/providerKind/getClient
      registry.ts              # AgentTypeRegistry
      session.ts               # RunSession/_MemoryView
      provider.ts              # StreamEvent/_DeepSeekTextFilter/BaseProvider/ResponsesProvider/...
      runtime.ts               # Agent/BaseAgent/采样循环/agent_runner/create_agent
      tools/user-input.ts      # RequestUserInputTool
    single-turn-chat.ts
    index.ts
  test/                        # 12 个 vitest 文件，对拍 pytest
```

| Python 源 | TS 模块 | 要点 |
|---|---|---|
| `pydantic_ai.messages`（第三方） | `messages.ts` | ModelRequest/ModelResponse + SystemPromptPart/UserPromptPart/TextPart/ToolCallPart/ToolReturnPart |
| `core/tool_types.py` | `tool-types.ts` | 纯数据容器 + `truncateText` |
| `core/tool.py` | `tool.ts` | `tool()` 工厂取代 `@tool`（无内省） |
| `memory/context_manager.py` | `memory/context-manager.ts` | token 不变量 + 版本化快照 |
| `memory/compaction.py` | `memory/compaction.ts` | LLM 摘要替换早期历史 |
| `memory/rollout.py` | `memory/rollout.ts` | append-only JSONL + 恢复 |
| `core/agent/types.py` | `agent/types.ts` | 枚举/错误层级/协议/数据类 |
| `core/agent/models.py` | `agent/models.ts` | 运行配置与上下文 |
| `core/agent/settings.py` | `agent/settings.ts` | 环境变量 → 配置 |
| `core/agent/registry.py` | `agent/registry.ts` | agent_type → factory |
| `core/agent/session.py` | `agent/session.ts` | 每 turn 受限视图 |
| `core/agent/provider.py` | `agent/provider.ts` | DSML 剥离 + Responses 流式 provider |
| `core/agent/runtime.py` | `agent/runtime.ts` | ReAct 采样循环 + 工具并发策略 |
| `core/agent/tools/user_input.py` | `agent/tools/user-input.ts` | 交互式提问工具 |
| `utils/single_turn_chat.py` | `single-turn-chat.ts` | 一次性单轮对话 |

## 4. 语言差异的等价替换（非字面移植）

1. **`@tool` 装饰器 → `tool(fn, opts)` 工厂**：Python 用 `inspect.signature` 从函数签名/类型注解/docstring 推导 input_schema；TS 无运行时内省。`name` 缺省取 `fn.name`，`description` 缺省取 `fn.name`（无 docstring），`inputSchema` 缺省空对象 schema。schema 自动推导相关 pytest 不移植。
2. **`asyncio.Event` → `CancellationToken`**：提供 `isSet()/set()/wait()`；`asyncio.CancelledError` → `CancelledError`（继承 `Error`，工具 `_execute` 据此区分「取消」与「业务错误」，不包装为 `ToolResult`）。
3. **`pydantic_ai.messages` → 自建 zod 联合**：`ModelMessage = ModelRequest | ModelResponse`（`kind` 判别），part 以 `part_kind` 判别；`ModelMessagesTypeAdapter` 提供 JSON 往返。rollout JSONL 格式为 TS 自洽（结构往返，非字节级与 Python 一致）。
4. **`invoke()` 退化为 async**：Python `asyncio.run()` 同步阻塞无 TS 等价；`invoke()` 返回 `Promise<ToolResult>`，测试改为 `await`。
5. **`traceback.format_exc()` → `Error.stack`**：写入 `ToolResult.data.traceback`。
6. **task.cancel() 中断传播**：Python `asyncio.Task.cancel()` 会在 await 点注入 `CancelledError`；TS 无等价物。取消经 `cancel` token 协作式传播（`provider.stream` 循环检查 `cancel.isSet()` 产出 error 事件）。`test_task_cancellation_propagates` 不移植。
7. **错误文案**：`{type(exc).__name__}: {exc}` → `${err.constructor.name}: ${err.message}`；pytest 里 `match="RuntimeError"` 等类型名断言按 TS 类型名（如 `Error`/`TypeError`）适配或改为匹配 message 子串。

## 5. 测试对拍

| pytest | vitest |
|---|---|
| `test/unit/test_tool.py` | `test/tool.test.ts` |
| `test/unit/test_context_manager.py` | `test/memory/context-manager.test.ts` |
| `test/unit/test_compaction.py` | `test/memory/compaction.test.ts` |
| `test/unit/test_rollout.py` | `test/memory/rollout.test.ts` |
| `test/unit/agent/test_types.py` | `test/agent/types.test.ts` |
| `test/unit/agent/test_settings.py` | `test/agent/settings.test.ts` |
| `test/unit/agent/test_registry.py` | `test/agent/registry.test.ts` |
| `test/unit/agent/test_session.py` | `test/agent/session.test.ts` |
| `test/unit/agent/test_deepseek_dsml.py` | `test/agent/provider.test.ts` |
| `test/unit/test_agent.py`（行为子集） | `test/agent/agent.test.ts` |
| `test/unit/test_single_turn_chat.py` | `test/single-turn-chat.test.ts` |
| `tests/test_core_public_api.py` | `test/public-api.test.ts` |

**验收标准**：`npm test`（vitest）全绿；`tsc --noEmit` 两包无错误；同输入同输出/同错误消息对拍。

## 6. 跨里程碑延后

`core/agent/agent_runtime.py`（`AgentRuntime` 门面）依赖 `app_server/thread_manager.py` 的 `RuntimeThreadManager`（属 M4）。故 M1 **不移植** `agent_runtime.ts`，对应 `test_agent_runtime*.py` / `test_kernel_facade.py` / `test_memory_restore.py` / `test_waits.py` 一并延后到 M4。

## 7. M2 Supervisor 设计（TODO）

> 状态：**暂为 TODO，后续专门设计**（2026-08-14 记录）。

M2 `@athena/research` 的 supervisor 是 TS 移植的重头戏，当前**不进入实现**。设计改进待后续单独进行。已识别待收敛点：

- 设计文档 `docs/supervisor_design.md`（Planner → Validator → Executor → Coordinator → Journal，SQLite 持久化）与当前 Python 实现（`research/supervisor/{supervisor,scheduler,policy,plans,prepare,recovery,experiment,validation,state,events}.py`，约 2700 行）存在结构与命名漂移；TS 设计前需先确认以哪一方为权威基线。
- 持久化/恢复（SQLite journal/lease/outbox/fencing）、组合根（cordis 装配 `ResearchRuntime`/Supervisor/服务）、一步式 Plan 状态机与调度、数据隔离与 `uv` 子进程脚本合同，均有 TS 侧需要重新决策的点。

## 7. 风险与开放项

- **真实 OpenAI SDK 接线**：`settings.get_client()` 在 M1 仅做「无 key 报错」+ 返回占位 client（真实 `chat.completions.create` 抛「deferred to M2」）。M2 接入 `openai` npm 包。
- **结构化输出适配**：`StructuredOutputType` 同时暴露 `name/modelJsonSchema`（provider 用）与 `parseJson/toJson`（runtime 用），统一复刻 pydantic `BaseModel` 的两种职责；M2 落地 zod schema 的适配器。
- **取消传播语义**：TS 协作式取消无法复刻 Python 的 `Task.cancel()` 强插异常；`agent_runtime` 落地时需定义统一取消契约。

# Implementation Plan: Task Understanding 多轮选择题澄清

关联设计：`docs/superpowers/specs/2026-08-18-task-understanding-clarification-design.md`

## 1. Problem summary

当前任务理解是一次 LLM JSON 调用，前端只显示最终预览；运行中的人类问题只支持自由文本。需要让 Agent 在任务理解阶段事无巨细地收集信息，通过前端 1/2/3 选择题弹窗交互，并把澄清结果写入 Handoff / 全局记忆。

## 2. Proposed solution

复用现有 `HumanRequestBroker` / `request_user_input` / `human_pending` / `human_reply` / `HumanRequestDialog`，把“问题”扩展为选择题；`parse_intent` 改为 Agent 驱动的多轮澄清 loop；澄清结果写 `TASK_CLARIFICATION.md` 并注册到 `state.handoff_refs`。

## 3. Acceptance criteria

1. `HumanRequestBroker` 支持 choices / custom / skip，且旧 `ask(prompt)` 调用仍返回纯文本。
2. `request_user_input` 工具 schema 支持 `choices`、`allow_custom`、`allow_skip`，并透传给 `ctx.ask_user`。
3. `parse_intent` 在需要澄清时阻塞等待前端回答，最多 8 问；每题 120s 超时按 skip；Agent 自主决定结束。
4. 澄清结束后工作区生成 `TASK_CLARIFICATION.md`，且 `state.handoff_refs["task_clarification"]` 指向其 artifact。
5. `_collect_handoff_texts()` 会把 task_clarification 注入 Ideator mailbox。
6. 前端弹窗：有 choices 时显示 1/2/3 按钮；支持自定义输入和跳过；旧纯文本问题仍正常。
7. LLM 失败时回退现有 heuristic，不阻塞前端。
8. 现有相关单测保持通过，新增测试覆盖上述行为。

## 4. Out of scope

- 不扩展 `TaskUnderstanding` 字段。
- 不新增 RPC 方法名；继续使用 `human_pending` / `human_reply`。
- 不改动 TUI。
- 不做前端题目回退/历史编辑。

## 5. Step-by-step implementation plan

### Step 1: 扩展 HumanRequestBroker

文件：`src/gui_gateway/human.py`

- pending 条目存储 `(request: dict, future)`，其中 request 含 `prompt`、`choices`、`allow_custom`、`allow_skip`。
- `ask(prompt, *, choices=None, allow_custom=True, allow_skip=True, timeout_s=120.0)`：
  - 旧调用 `ask(prompt)` 行为不变；
  - 阻塞等待 future，超时按 skip 返回 `"skip"`。
- `pending()` 返回旧字段 + 新字段；choices 缺省为 `None`，前端可识别。
- `reply(request_id, answer=None, *, choice=None, skip=False)`：
  - `choice` → `"choice:<value>"`；
  - `answer` → `"text:<answer>"`（旧行为 `answer` 直接返回原文本以保证兼容）；
  - `skip` → `"skip"`。
  - 注意保持旧测试 `reply(request_id, answer="done")` 返回 `"done"`。

### Step 2: 扩展 request_user_input 工具

文件：`src/athena/core/agent/tools/user_input.py`

- input schema 增加：

```json
"choices": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label", "value"]}},
"allow_custom": {"type": "boolean", "default": true},
"allow_skip": {"type": "boolean", "default": true}
```

- `execute` 调用：

```python
answer = await ctx.ask_user(
    input["prompt"],
    choices=input.get("choices"),
    allow_custom=input.get("allow_custom", True),
    allow_skip=input.get("allow_skip", True),
)
```

文件：`src/athena/core/tool_types.py`

- `AskUser` 改为 `Callable[..., Awaitable[str | None]]`，允许 broker.ask 扩展参数。

### Step 3: handler 支持 choice/skip 回答

文件：`src/gui_gateway/handler.py`

`human_reply` 分支：

```python
self._broker.reply(
    request_id=...,
    answer=params.get("answer"),
    choice=params.get("choice"),
    skip=bool(params.get("skip")),
)
```

### Step 4: GuiService.parse_intent 多轮澄清 loop

文件：`src/athena/gui/service.py`

- 构造函数接收 `broker: HumanRequestBroker | None = None`；`GuiRequestHandler` 创建 `GuiService(runtime, broker=self._broker)`。
- 新增 system prompt `_CLARIFYING_UNDERSTANDING_SYSTEM`，要求 LLM 返回两类 JSON 之一：
  - `{"done": true, "understanding": {...}}`
  - `{"done": false, "question": "...", "choices": [{"label": ..., "value": ...}], "allow_custom": true, "allow_skip": true}`
- `parse_intent` 循环：
  - 维护 `messages` 列表（system + 用户历史 + Q&A）；
  - 每次 LLM 调用后解析；
  - `done=false` → `broker.ask(...)`，把回答追加为 user message，`asked += 1`；
  - `done=true` 或 `asked >= CLARIFY_MAX_QUESTIONS` → 结束；
  - LLM 异常 → 现有 heuristic fallback。
- 结束后生成 `TASK_CLARIFICATION.md`（原始 task、Q&A、final understanding、未解决项），`put_text` 到 store，写入 `state.handoff_refs["task_clarification"]`，保存 state。
- 保持返回值是最终 `TaskUnderstanding` dict，前端预览卡逻辑不变。

### Step 5: supervisor prompt 增加选择题追问

文件：`src/athena/core/agent/prompts/supervisor_agent.md`

在现有 “When a Kaggle download reports...” 之前增加：

```markdown
During PREPARE/SEARCH, when a critical fact is unknown:
- call request_user_input with choices=[...] whenever possible;
- ask ONE question at a time;
- stop asking once the decision no longer depends on the answer.
```

### Step 6: handoff 收集加入 task_clarification

文件：`src/athena/research/agent_turn_runner.py`

`_collect_handoff_texts()` 最前面或最后增加：

```python
clarification_ref = rt.state.handoff_refs.get("task_clarification")
if clarification_ref:
    try:
        texts.append(await rt._store.get_text(clarification_ref))
    except Exception:
        pass
```

### Step 7: 前端类型与 RPC wrapper

文件：`athena-gui/src/lib/tauri-bridge.ts`

```ts
export interface ClarificationChoice { label: string; value: string; }
export interface HumanRequest {
  request_id: string;
  prompt: string;
  choices?: ClarificationChoice[] | null;
  allow_custom: boolean;
  allow_skip: boolean;
}
export function humanChoice(requestId: string, value: string): Promise<{replied: boolean}> { ... }
export function humanSkip(requestId: string): Promise<{replied: boolean}> { ... }
```

- `humanChoice` 调 `rpc("human_reply", { request_id, choice: value })`。
- `humanSkip` 调 `rpc("human_reply", { request_id, skip: true })`。
- `humanReply` 保持兼容，调 `{ request_id, answer }`。

### Step 8: 前端弹窗渲染选择题

文件：`athena-gui/src/components/shell/HumanRequestDialog.tsx`

- props 增加 `onChoice(requestId, value)`、`onSkip(requestId)`。
- 有 `choices` 时渲染 1/2/3 按钮；
- `allow_custom` 时显示文本框，旧 `onAnswer` 保持；
- `allow_skip` 时显示“跳过”。

### Step 9: usePipeline 启动前轮询与回答

文件：`athena-gui/src/hooks/usePipeline.ts`

- 增加状态 `awaitingIntent`：
  - `sendPrompt` 调用 `sendMessage` 前置 true，返回后置 false；
  - 该状态下 `humanPending` 轮询生效（当前只在 running 时轮询）。
- `answerHuman` 支持 choice/skip：
  - choice → `humanChoice`；
  - skip → `humanSkip`；
  - 文本 → 现有 `humanReply`。

### Step 10: 测试

新增/修改：

- `tests/test_gui_gateway_handler.py`：
  - broker choices 存储；
  - choice/text/skip 三种回复；
  - 旧 `answer` 兼容。
- `test/unit/test_agent.py` 或新 test：
  - `request_user_input` 新 schema 与 ask_user 参数透传。
- `tests/test_gui_protocol_contract.py`：
  - 若协议方法列表无变化则不动；仅确认 `human_pending/human_reply` 仍在。
- `src/athena/gui/service.py` 的 parse_intent 测试：
  - fake LLM 返回 question → final；
  - 超时/上限/失败回退。
- 前端组件测试：
  - `HumanRequestDialog` 渲染 1/2/3、自定义、跳过。

## 6. Repo impact

后端：

- `src/gui_gateway/human.py`
- `src/gui_gateway/handler.py`
- `src/athena/core/agent/tools/user_input.py`
- `src/athena/core/tool_types.py`
- `src/athena/gui/service.py`
- `src/athena/core/agent/prompts/supervisor_agent.md`
- `src/athena/research/agent_turn_runner.py`

前端：

- `athena-gui/src/lib/tauri-bridge.ts`
- `athena-gui/src/components/shell/HumanRequestDialog.tsx`
- `athena-gui/src/hooks/usePipeline.ts`

测试：

- `tests/test_gui_gateway_handler.py`
- `test/unit/test_agent.py` 或新 test
- 新 `test/unit/gui/test_parse_intent_clarify.py`
- `athena-gui/src/components/__tests__/human-request-dialog.test.tsx`（可选）

## 7. Risks and considerations

- 前端 `sendMessage` Promise 在澄清期间长期 pending：需在 WebSocket/Tauri 侧确认并发轮询可用；若传输层单工，改为先返回 `{clarifying: true}`，再复用 event 流。
- 多轮 LLM 调用成本：上限 8 问 + 每题 120s 超时。
- 兼容性：旧纯文本人类问题必须保持可用（Kaggle rules 确认、SEARCH 预算询问）。
- 会话切换/断点续传：`handoff_refs` 持久化，重启不重复澄清。

## 8. Testing notes

- 后端用 fake broker / fake LLM 做纯单测，不触网。
- 前端组件测试用 jsdom；`humanPending` 轮询用 fake timers。
- 手工验收路径：
  1. 输入一个模糊任务 → 前端出现 1/2/3 弹窗；
  2. 连续回答后出现最终 IntentPreviewCard；
  3. 确认启动后，PREPARE 运行中仍可能弹出问题；
  4. 恢复会话时不再重复澄清。

## 9. Rollback/migration notes

- `AskUser` 和 broker 均保持旧调用兼容，`human_reply(answer=...)` 返回原文。
- 如传输层不支持 pending 轮询，可在 `parse_intent` 保留旧一次调用路径作为 fallback flag。
- `task_clarification` handoff 失败只降级为不注入，不影响启动。

## 10. Open questions

无。实现中若发现 Tauri invoke 对长期 pending RPC 有限制，按 Risks 中 fallback 处理。

# Implementation Plan: Task Understanding 多轮选择题澄清

关联设计：`docs/superpowers/specs/2026-08-18-task-understanding-clarification-design.md`

## 目标

复用现有 `HumanRequestBroker` / `request_user_input` / `human_pending` / `human_reply` / `HumanRequestDialog`，把任务理解升级为前端 1/2/3 选择题多轮澄清；澄清结果写 `TASK_CLARIFICATION.md` 并进入现有 `handoff_refs`。

## 实施步骤

### 1. `src/gui_gateway/human.py`

- `_pending` 值改为 `(request: dict, future)`，request 含 `prompt / choices / allow_custom / allow_skip`。
- `ask(prompt, *, choices=None, allow_custom=True, allow_skip=True, timeout_s=120.0)`。
- `pending()` 原样返回 request dict（旧字段兼容，新字段为空时前端忽略）。
- `reply(request_id, answer=None, *, choice=None, skip=False)`：
  - 旧路径 `answer="done"` 仍返回 `"done"`；
  - choice → `"choice:<value>"`；skip → `"skip"`。

### 2. `src/athena/core/agent/tools/user_input.py`

- schema 增加 `choices`（`[{label, value}]`）、`allow_custom`、`allow_skip`。
- `execute` 把三者透传给 `ctx.ask_user(prompt, choices=..., allow_custom=..., allow_skip=...)`。
- `src/athena/core/tool_types.py` 中 `AskUser` 改为 `Callable[..., Awaitable[str | None]]`。

### 3. `src/gui_gateway/handler.py`

- `human_reply` 调 `broker.reply(request_id, answer, choice, skip)`，字段缺省 None。

### 4. `src/athena/gui/service.py::parse_intent`

- 保留现有 heuristic fallback 和 TaskUnderstanding 返回类型。
- 把单次 LLM 调用改为小循环：

```python
messages = [system, *user_history]
for _ in range(CLARIFY_MAX_QUESTIONS):
    payload = await _ask_understanding_llm(messages)   # 新私有 helper
    if payload.get("done"): break
    answer = await broker.ask(payload["question"], choices=payload.get("choices"), ...)
    messages.append({"role": "user", "content": answer})
```

- LLM 返回两类 JSON：
  - `{"done": true, "understanding": {...}}`
  - `{"done": false, "question": "...", "choices": [...]}`
- 循环结束后：
  - `clarification_md = _render_clarification(task, qa_pairs, understanding)`
  - `ref = await runtime._store.put_text(clarification_md)`
  - `runtime.state.handoff_refs["task_clarification"] = ref; runtime.state.save(...)`
- `GuiRequestHandler` 构造 `GuiService(runtime, broker=self._broker)`。

### 5. `src/athena/core/agent/prompts/supervisor_agent.md`

增加：

```markdown
When a critical fact is unknown, call request_user_input with choices=[...]
when possible; ask one question at a time and stop when the answer no longer
affects the decision.
```

### 6. `src/athena/research/agent_turn_runner.py::_collect_handoff_texts`

```python
ref = rt.state.handoff_refs.get("task_clarification")
if ref:
    texts.append(await rt._store.get_text(ref))
```

### 7. 前端

- `tauri-bridge.ts`：
  - `HumanRequest` 增加 `choices? / allow_custom / allow_skip`；
  - 新增 `humanChoice(requestId, value)` 和 `humanSkip(requestId)`，复用 `human_reply` RPC。
- `HumanRequestDialog.tsx`：
  - 有 choices → 渲染 1/2/3 按钮；
  - 允许自定义输入和跳过；旧文本问答保留。
- `usePipeline.ts`：
  - `sendPrompt` 在 `sendMessage` pending 期间置 `awaitingIntent=true`；
  - 该状态也触发 `humanPending` 轮询；
  - 回复时 choice/skip 走新 wrapper，文本走旧 `humanReply`。

### 8. 测试

- `tests/test_gui_gateway_handler.py`：choice/text/skip 与旧 answer 兼容。
- `test/unit/test_agent.py`：`request_user_input` choices 透传。
- 新 `test/unit/gui/test_parse_intent_clarify.py`：多轮问题、超时、上限、LLM 失败回退。
- 前端 `HumanRequestDialog`：1/2/3、自定义、跳过。

## 验收标准

1. 旧文本人类问题不回退。
2. 启动前可多轮 1/2/3 澄清并生成最终预览。
3. 澄清结果进入 `TASK_CLARIFICATION.md` + `handoff_refs`，Ideator 可见。
4. LLM 失败回退 heuristic，前端不被卡住。

## 不做

- 不改 TaskUnderstanding 字段。
- 不新增 RPC 方法名。
- 不改 TUI。

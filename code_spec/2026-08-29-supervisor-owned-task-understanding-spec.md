# Spec：任务理解统一收归 Supervisor（简化版）

日期：2026-08-29
状态：已按简化方案实施
依据：`docs/前端分析.md`

---

## 1. 问题

GUI 新任务原先会同时触发两条理解链路：

1. `parse_intent`（前端发起，Python GUI Service 执行独立 LLM 澄清）；
2. `start_search → start_task → Supervisor maybe_run_task_understanding`。

两条链路互不通信，导致重复任务理解、重复提问、状态载体不一致。

---

## 2. 简化后的机制

**所有任务理解只由后端 Supervisor 执行，前端只做输入、展示和问答呈现。**

```text
用户输入（新任务）
  → 前端 start_search
  → ResearchRuntime.start_task
  → Supervisor 任务理解
       → request_user_input（需要时）
       → configure_kaggle（需要时）
       → record_task_understanding
  → state / output 事件
  → 前端渲染预览与澄清弹窗
```

运行中指导：

```text
用户输入（运行中）
  → 前端 message / sendControl
  → Supervisor.message
  → 普通 Supervisor 回合
```

---

## 3. 边界条件

1. `start_search / start_task` 是唯一新任务入口。
2. `state.task_understanding` 只能由 Supervisor 写入。
3. 澄清只能走 `request_user_input → human_pending / human_reply`。
4. Kaggle 决策保留在 Supervisor 任务理解回合内，不能跳过。
5. 前端不调用任何独立任务理解 RPC。
6. 前端不单独生成理解结果，只等 `state.task_understanding` 事件更新预览。

---

## 4. 已实施的代码改动

### 前端

- 删除 `sendMessage()` / `parse_intent` 调用。
- 新任务只调用 `start_search`。
- 新增占位 intent-preview，等待后端 `state.task_understanding` 填充。
- 删除 `awaitingIntent` 状态。
- 会话标题由后端理解结果或原始任务文本生成。

### Tauri / Rust

- 删除 `send_message` 命令。
- 保留 `message`，用于运行中指导。

### Python GUI Gateway / Service

- 删除 `parse_intent`。
- 删除其独立 LLM 澄清循环、heuristic 回退。
- `SUPPORTED_METHODS` 与 dispatch 同步移除。

### Supervisor 后端

- 在 `ask_user` 外层记录任务理解期间的澄清问答。
- 在任务理解完成后由 Supervisor 工作流生成 `TASK_CLARIFICATION.md`，写入 `handoff_refs["task_clarification"]`。
- Ideator 的 `_collect_handoff_texts` 恢复读取该 handoff。

### 测试

- 删除 `parse_intent` 旧单测。
- 更新协议契约测试，移除 `parse_intent` / `send_message`。
- 前端测试改为验证“只调用 start_search，不调用 parse_intent”。
- 新增后端单测：Supervisor 任务理解完成后写入一份澄清 handoff。

---

## 5. 未纳入本次简化

- **前端输入路由重构**：保留当前“运行中走 sendControl、否则 start_search”的最小逻辑；后续可改为显式两个动作。
- **实验管理**：`experiment_transition` / `experiment_set_sota` 保持现状，另行决策。

---

## 6. 验收

- 新任务不出现两条任务理解链路。
- 运行中普通消息仍进入 Supervisor，不触发新任务。
- Kaggle 任务仍可由 Supervisor 配置。
- 协议中不存在 `parse_intent`。
- `tsc --noEmit` 通过。
- Python 协议契约测试通过。

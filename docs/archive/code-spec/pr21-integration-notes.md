# PR #21 合并前分析：机制差异与对接所需代码

日期：2026-08-29
PR：https://github.com/FakeEyes2Wo/Athena/pull/21
分支：`fix/gui-state-suspend`
Worktree：`.worktrees/pr21-review`
当前仅下载对比，未合并。

---

## 1. PR 机制内容（按 commit）

### 1.1 RPC 错误回传真实原因

- `gui_gateway/transport.py`：异常不再统一压成 `"request failed"`。
- 回传 `redact(str(exc))`，为空时用异常类型名。
- `RpcError` 附加 `data.exception` / `data.method`。
- 前端可区分「会话删除失败 / runtime 已关闭 / 路径非法」。

### 1.2 修复 GUI 会话流消息丢失与重复

- `OutputEvent` 增加 `message_id`。
- `RuntimeEvents` 给同一条 agent 消息的所有 delta / flush 共用一个 message id。
- 前端 `usePipeline` 按 `message_id` upsert：
  - 实时 delta 追加；
  - transcript 整条替换。
- `forward_run_events` / `wait_run_events` / `wait_run_with_heartbeat` 增加序列游标：
  - 心跳重入从上次成功转发的 sequence 续传，不重放整段 journal。
- `ResponsesProvider` 在函数调用边界前先释放 DSML 过滤器扣留的文本尾部，修复消息缺尾。

### 1.3 修复会话切换后的运行态悬空

- `PhaseMachine.suspend()`：`RUNNING → WAITING` 并落盘。
- `ResearchRuntime.suspend()` 暴露给 GUI 会话切换前调用。
- `_swap_runtime()` 在 `aclose()` 前 `suspend()`，避免磁盘留下假 RUNNING。
- `handler` 加载侧：不自动续跑的持久化 RUNNING 降级为 WAITING；PREPARE 不自动续跑。
- `/resume` 判据从 `_started` 改为 `_started or state.json 存在`。
- 前端 `runActive` 把后端 `running/paused` 也算作运行证据。

### 1.4 修复会话列举、删除与恢复

- 空工作区不再硬编码列 `default`，只有有 transcript / state.json 才算存在。
- `default` 可删除，语义改为“重置默认会话”。
- 空白命名会话由后端判定并回收，前端不再猜测。
- `GuiState` 增加 `last_sessions`（工作区 → 上次会话），挂载时恢复上次会话。
- `sessions_list` 增加 `active`；`session_switch` 增加 `sessions`。

---

## 2. 与当前机制的重叠面

当前未提交工作区已包含“任务理解统一收归 Supervisor”的改动：

- 前端已删除 `parse_intent` / `sendMessage`。
- Python `GuiService` 已删除 `parse_intent` 独立链路。
- `handler.py` 已从 `SUPPORTED_METHODS` 与 dispatch 删除 `parse_intent`。
- 后端新增 Supervisor 工作流的 `TASK_CLARIFICATION.md` 生成。
- `runtime_control.py` 已包含任务澄清 + traceback 输出。
- `runtime.py` 已包含 ask_user 记录包装。

PR #21 不包含这些改动，因此合并时要“保留当前任务理解收敛，叠加 PR 的 GUI 会话/消息修复”。

---

## 3. 需要补充/保留的代码

### 3.1 `src/gui_gateway/handler.py`

**保留当前：**
- 从 `SUPPORTED_METHODS` 删除 `parse_intent`。
- 从 dispatch 删除 `parse_intent` 分支。

**叠加 PR：**
- `_session_activity`
- `_suspend_runtime`
- `_resume_running_session` 新逻辑（PREPARE 不续跑、悬空 RUNNING 降级）
- `set_project_root` 重置当前会话
- `session_switch` 返回 sessions、回收空白会话、记住 last-active
- `_discard_blank_session`
- `_remember_session`
- `_session_ids` 后端判定
- `sessions_list` 返回 `active`
- `session_delete` 支持删除 default 并重置默认会话
- `_reset_default_session`

### 3.2 `src/athena/research/runtime.py`

**保留当前：**
- ask_user 记录包装 / `clarification_qa`。

**叠加 PR：**
- `ResearchRuntime.suspend()` 方法。

### 3.3 `src/athena/research/runtime_control.py`

**保留当前：**
- `render_task_clarification`
- `persist_task_clarification`
- 任务理解失败输出 traceback

**叠加 PR：**
- import `Path`
- `/resume` 使用 `resumable = runtime._started or Path(runtime._state_path).is_file()`

### 3.4 `src/athena/research/supervisor/phases.py`

**叠加 PR：**
- `PhaseMachine.suspend()`

当前该文件已有 traceback 输出改动，不冲突。

### 3.5 `src/athena/research/runtime_events.py`

**叠加 PR：**
- `message_id` 透传
- Agent buffer 保存 `message_id`
- 空串/空白处理

当前未改动，可直接应用。

### 3.6 `src/athena/research/supervisor/events.py`

**叠加 PR：**
- `OutputEvent.message_id`
- `EventProjector` 支持 message_id

### 3.7 `src/athena/research/agent_turn_common.py`

**叠加 PR：**
- `forwarded` 游标与 `wait_run_events` 续传参数

### 3.8 `src/athena/research/supervisor/plans.py`

**叠加 PR：**
- `SequenceSink`
- `forward_run_events` / `wait_run_events` 的 after_sequence / on_sequence

### 3.9 `src/gui_gateway/transport.py`、`state_store.py`、`src/athena/app_server/protocol.py`

直接应用 PR 改动，与当前工作区无冲突。

### 3.10 `src/athena/core/agent/provider.py`

直接应用 PR 的 DSML tail release 改动，与当前工作区无冲突。

---

## 4. 前端合并要点

### `athena-gui/src/hooks/usePipeline.ts`

**保留当前：**
- 删除 `sendMessage` / `parse_intent` 调用。
- 新任务只调用 `start_search`。
- 预览占位 + 后端 `state.task_understanding` 填充。
- 删除 `awaitingIntent`。

**叠加 PR：**
- `applyPipelineEvent` 增加 `replay` 参数与 `message_id` upsert。
- 会话列表改为后端驱动：
  - `applySessions`
  - `sessionsList` 返回 `active`
  - `sessionSwitch` 返回 `sessions`
- 删除前端 `currentSessionBlank` 判断，交给后端回收。
- `newSession` / `switchSession` / `deleteSession` 使用后端返回列表。
- `runActive` 包含 `viewModel.status === "running" / "paused"`。

### `athena-gui/src/lib/tauri-bridge.ts`

**保留当前：**
- 删除 `sendMessage` / `parse_intent`。
- `TaskUnderstanding.needs_configuration` 可选。

**叠加 PR：**
- `SessionRecord.message_id?`
- `sessionsList()` 返回 `{ sessions, active }`
- `sessionSwitch()` 返回 `{ records, sessions? }`
- `sessionDelete()` 支持 default

### 前端测试

- 保留当前“无 parse_intent”断言。
- 合并 PR 新增的 message identity / session tests。
- 删除旧 `sendMessage` 相关测试。

---

## 5. 冲突解决策略

1. 以 PR 为基线应用 GUI 会话与消息修复。
2. 在此基础上重新应用“任务理解统一收归 Supervisor”的差异：
   - 删除 `parse_intent` 协议 / Rust / TS / Python；
   - 保留 `TASK_CLARIFICATION.md` 后端生成；
   - 保留 `start_search` 唯一新任务入口。
3. 对 `usePipeline.ts`、`handler.py`、`runtime_control.py`、`runtime.py` 做手动三方合并。
4. 保留 `tests/test_gui_protocol_contract.py` 中 `parse_intent` 已删除的契约。
5. 删除 PR 工作区中仍存在的 `test_gui_service_clarify.py` / `test_gui_service_history.py`。

---

## 6. 合并后测试计划

- `pytest tests/test_gui_protocol_contract.py`
- `pytest test/unit/research/test_task_clarification.py`
- PR 新增测试：
  - `test/unit/research/test_message_identity.py`
  - `test/unit/research/test_run_event_forwarding.py`
  - `test/unit/research/supervisor/test_phase_suspend.py`
  - `test/unit/gui/test_state_store.py`
  - `tests/test_gui_gateway_handler.py`
  - `tests/test_gui_gateway_transport.py`
- `npx tsc --noEmit`
- 前端 vitest（若环境允许）

---

## 7. 合并保留方案

### 目标

合并 PR #21 的 GUI 会话/消息修复，同时保留当前“任务理解统一收归 Supervisor”的改动。

### 保留原则

- **保留 PR 的**：会话 suspend/resume、message_id 消息身份、heartbeat 续传游标、RPC 错误真实原因、会话列表/删除/恢复。
- **保留当前的**：删除 `parse_intent` / `send_message`；`start_search` 唯一新任务入口；Supervisor 生成 `TASK_CLARIFICATION.md`；`ask_user` 澄清记录；前端预览由后端 `state.task_understanding` 填充。
- **不保留**：旧 `parse_intent` 独立 LLM/澄清链路、旧 heuristic、前端 `sendMessage`、旧 `parse_intent` 单测。

### 文件保留矩阵

| 文件 | 保留 PR | 保留当前 | 合并动作 |
|---|---|---|---|
| `gui_gateway/handler.py` | session 管理全套 | 删除 `parse_intent` | 先应用 PR，再重新删除 `parse_intent` |
| `research/runtime.py` | `suspend()` | ask_user 记录包装 | 两者都保留 |
| `research/runtime_control.py` | Path + `/resume` 判据 | TASK_CLARIFICATION + traceback | 两者都保留 |
| `research/supervisor/phases.py` | `PhaseMachine.suspend()` | traceback 输出 | 两者都保留 |
| `research/runtime_events.py` | message_id / agent buffer | 无冲突 | 应用 PR |
| `research/supervisor/events.py` | `OutputEvent.message_id` | 无冲突 | 应用 PR |
| `research/agent_turn_common.py` | sequence 游标 | 无冲突 | 应用 PR |
| `research/supervisor/plans.py` | `SequenceSink` | 无冲突 | 应用 PR |
| `gui_gateway/transport.py` | 真实错误信息 | 无冲突 | 应用 PR |
| `gui_gateway/state_store.py` | `last_sessions` | 无冲突 | 应用 PR |
| `app_server/protocol.py` | RpcError 语义 | 无冲突 | 应用 PR |
| `core/agent/provider.py` | DSML tail release | 无冲突 | 应用 PR |
| `athena/gui/service.py` | 无 PR 改动 | 已删除 `parse_intent` | 保留当前 |
| `research/agent_turn_runner.py` | 无 PR 改动 | TASK_CLARIFICATION 读取 | 保留当前 |
| `usePipeline.ts` | 消息 identity / 后端会话 / runActive | 删除 sendMessage 与独立理解 | 手动三方合并 |
| `tauri-bridge.ts` | active / record message_id | 删除 sendMessage | 手动三方合并 |

### 合并步骤建议

1. 先把当前未提交改动做成一个 WIP commit 或 stash，避免合并时丢失。
2. 以当前 `main` 为基线合并 `origin/pr-21`。
3. 解决冲突时按上方矩阵取舍。
4. 合并后全局 grep：
   - `parse_intent`
   - `send_message`
   - `sendMessage`
   确认无残留。
5. 删除 PR 工作区中仍存在的旧 `parse_intent` 测试：
   - `tests/test_gui_service_clarify.py`
   - `tests/test_gui_service_history.py`
6. 运行：
   - `pytest tests/test_gui_protocol_contract.py`
   - `pytest test/unit/research/test_task_clarification.py`
   - PR 新增系列测试
   - `npx tsc --noEmit`

---

## 8. 当前状态

- 已 `git fetch origin pull/21/head → origin/pr-21`
- 已创建 detached worktree：`.worktrees/pr21-review`
- **未合并、未推送**

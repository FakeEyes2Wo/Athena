# GUI 会话链路的四类缺陷：根因、复现与修复方案

Status: current
Owner: Athena maintainers
Last verified: 2026-08-28
Source of truth: `src/gui_gateway/handler.py`, `src/gui_gateway/transport.py`,
`src/athena/research/runtime_events.py`, `src/athena/research/supervisor/{phases,plans}.py`,
`athena-gui/src/hooks/usePipeline.ts`, `athena-gui/src/components/{conversation,shell}/`

2026-08-28 从 GUI 侧报上来四个现象：state 显示与实际不符、同工作区两个会话不能并行、
Agent 消息被工具输出吞掉、部分会话删不掉且空工作区凭空占一行。逐条追到根因后发现它们
**共用同一个架构前提**——网关只持有一个 `ResearchRuntime`，会话切换靠"关掉旧的、重建新的"
实现，而这个过程既不落盘运行态、也不给前端任何"这是新会话的第一帧"的边界信号。

本文按可独立开分支的粒度拆成四节。每节给出：症状、根因、证据、复现、修复方案、
涉及文件、验收。第五节是跨问题的可观测性问题。

建议修复顺序与分支：

| 顺序 | 分支 | 节 | 理由 |
|---|---|---|---|
| 1 | `fix/research-journal-replay` | 三.1 | 独立、改动最小、不依赖其它三条 |
| 2 | `fix/gui-message-identity` | 三.2 | 契约改动（`OutputEvent.message_id`），前后端一起动 |
| 3 | `fix/gui-session-state-alignment` | 一 | 依赖 2 的事件契约稳定后再动 state 语义 |
| 4 | `fix/gui-session-lifecycle` | 四 | 会话增删列举语义，独立于运行态 |
| 5 | `feat/gui-multi-session-runtime` | 二 | 架构改动，前四条修完才好动 |

---

## 一、state 前后端不对齐：显示"运行中"，实际什么都没跑

### 症状

点开一个之前跑过的会话，顶栏显示 `PREPARE · 运行中`，但没有任何新输出，
暂停按钮点了没反应。用户预期是"暂停中"。

### 根因

`Supervisor.stop()`（[`phases.py:319`](../src/athena/research/supervisor/phases.py#L319)）
只做 `set_stopped(True)` + 中断 Plan + reap Agent，**从不写 `state.status`，也不落盘**。
只有显式的 `request_stop()`（`/stop` RPC）才会写 `STOPPED`。

而 `session_switch` / `set_project_root` 都走
[`handler.py:152`](../src/gui_gateway/handler.py#L152) 的 `_swap_runtime`，它无条件
`await self._runtime.aclose()` → [`runtime.py:816`](../src/athena/research/runtime.py#L816)
→ `Supervisor.stop()`。

于是：**切走一个正在跑的会话 = 杀掉它的 supervisor，但磁盘上仍是 `status="RUNNING"`。**

切回来时新 runtime 从 `state.json` 读出 `RUNNING`（`ResearchState.load` 没有任何
reconciliation），`RuntimeEvents.subscribe` 立刻把这份快照推给前端，
`RUNTIME_STATUS_MAP["RUNNING"] → "running"`，`RunControls` 就显示"运行中"。

第二层放大：[`handler.py:168`](../src/gui_gateway/handler.py#L168) 的
`_resume_running_session` 只在 `phase ∈ {SEARCH, VALIDATE}` 时才真的 `start()`。
所以 **phase=PREPARE 的会话是纯悬空态**：显示运行中，但两边都没人跑它。
phase=SEARCH 则是另一种不对齐——用户只是点进去看一眼，它被静默重启了。

### 证据（已实测）

```
after aclose, state.json = RUNNING PREPARE
reloaded runtime reports  = RUNNING PREPARE
```

构造方式：建一个 `ResearchRuntime`，把 `state.status` 置为 `RUNNING`、`phase` 置为
`PREPARE` 并 `save()`，`await aclose()` 之后重新 `ResearchState.load()`，状态未变。

### 复现

1. 新建会话 A，发一个任务 → 进入 PREPARE，`status=RUNNING` 落盘。
2. 点侧栏另一个会话 B（或切工作区，或从"实验/报告"模块切回"会话"模块）。
3. 点回会话 A。顶栏 `PREPARE · 运行中`，无输出，暂停按钮可点但无效。

### 修复方案

1. **写入侧（治本）**：给 `Supervisor` 加 `suspend()`，把 `RUNNING → WAITING` 并
   `_persist_state()`；`_swap_runtime` 在 `aclose()` **之前**调用它。
   用 `WAITING`（前端映射为"暂停"）而不是 `STOPPED`——后者会让
   `rearm_if_terminal`（[`runtime_control.py:127`](../src/athena/research/runtime_control.py#L127)）
   把断点续传语义弄乱。
2. **加载侧（防御性第二层）**：`_resume_running_session` 判定"不续跑"时，把内存
   `state.status` 降级为 `WAITING` 并落盘，保证推给前端的快照永远等于真实运行状态。
3. **决定 PREPARE 的语义**：要么让 `_resume_running_session` 也覆盖 PREPARE，
   要么在注释里写明"PREPARE 不续跑，一律降级为 WAITING 等用户手动继续"。
   现在是两边都不管的悬空态。

### 涉及文件

- `src/athena/research/supervisor/phases.py`（新增 `suspend`）
- `src/athena/research/supervisor/supervisor.py`（门面转发）
- `src/gui_gateway/handler.py`（`_swap_runtime`、`_resume_running_session`）

### 验收

- 单测：runtime 处于 `RUNNING` 时 `aclose()`，重新 `ResearchState.load()` 得到 `WAITING`。
- 单测：`_resume_running_session` 对 `PREPARE/RUNNING` 的持久化状态不续跑，且落盘为 `WAITING`。
- 契约测试保持绿：`tests/test_gui_protocol_contract.py`。

---

## 二、同一工作区不能同时用两个会话

### 症状

会话 A 跑起来后点会话 B，A 就停了；切回 A 输出不再增长（PREPARE）或被静默重启（SEARCH）。

### 根因

网关是**单 runtime 单例**：

- [`handler.py:139`](../src/gui_gateway/handler.py#L139)：`GuiRequestHandler` 只有一个
  `self._runtime`。
- `session_switch` → `_swap_runtime` → **先 `aclose()` 旧的再建新的**。
- [`transport.py:53-69`](../src/gui_gateway/transport.py#L53-L69)：`WebSocketTransport.handle`
  只维护一个 `subscription_id`，dispatch 后发现 `handler.runtime` 换了就把订阅迁过去。
- 前端同样只有一个 `usePipeline` view model。

所以"两个会话同时跑"在架构上不成立，而且切会话会**静默杀死**正在跑的那个
（再叠加第一节的脏 `RUNNING`）。

放大器：[`AppShell.tsx:44-53`](../athena-gui/src/components/shell/AppShell.tsx#L44-L53)
从别的模块切回"会话"模块时会调 `switchSession(pipeline.currentSessionId)`——
**对同一个会话也会整段重建 runtime**。它有 `status !== "running"` 的保护，但一旦
status 因为第一节的缺陷而失真，这个保护就失效。

### 复现

1. 会话 A 发任务，等它进入 SEARCH 并有输出。
2. 点会话 B。
3. 点回会话 A：要么输出停在切走那一刻，要么被 `_resume_running_session` 静默重启。

或者更轻的一条：会话跑着的时候点"研究树"模块再点回"会话"模块。

### 修复方案

按代价递增，可以分两个 PR 落在同一分支上。

**第一步（止血，无架构改动）**

- `session_switch` 在 `session_id == self._current_session_id` 时直接返回 transcript，
  不执行 `_swap_runtime`。
- `AppShell` 那个 effect 改用一个只读 RPC（例如 `session_transcript`）重放对话，
  而不是 `session_switch`。新增 RPC 要同步 `SUPPORTED_METHODS`、`GuiService`、
  `tauri-bridge.ts` 和 `tests/test_gui_protocol_contract.py`。

**第二步（真多会话）**

- `GuiRequestHandler` 把 `self._runtime` 换成 `dict[session_id, ResearchRuntime]`，
  `session_switch` 只切换"当前订阅目标"，不关闭旧 runtime。
- `_swap_runtime` 现在还兼着 `state_root.mkdir(parents=True, exist_ok=True)`
  （[`handler.py:159-161`](../src/gui_gateway/handler.py#L159-L161)），拆分时别把它丢了，
  否则 `sessions_list` 只列已存在目录，新会话刷新后会消失。
- 事件帧加 `session_id`，`WebSocketTransport` 同时订阅多个 runtime；
  前端按 `session_id` 分桶存 view model。
- 关闭语义要一起定：什么时候真正 `aclose()` 一个后台会话（进程退出、显式停止、
  还是 LRU 驱逐）。

**资源边界（设计阶段就要想清楚）**

每个 runtime 各自持有 `GpuPool`、`ExecutionRuntime`、git repo 与 worktree。
并行两个会话意味着并发的 worktree 和显存租约，需要明确上限与拒绝策略，
否则第二个会话会在租约上死等。

### 涉及文件

- `src/gui_gateway/handler.py`、`src/gui_gateway/transport.py`
- `src/athena/gui/service.py`（若事件帧加 `session_id`）
- `athena-gui/src/hooks/usePipeline.ts`、`athena-gui/src/components/shell/AppShell.tsx`
- `tests/test_gui_protocol_contract.py`

### 验收

- 单测：`session_switch` 到当前会话不触发 `aclose()`。
- 单测：会话 A 有在跑的 Plan 时切到 B，A 的 runtime 仍然存活且 status 不变。
- 手测：两个会话各跑一个任务，互不打断，侧栏两行都能看到各自的进度。

---

## 三、Agent 消息被已调用的 tool "撤删"

这条是**两个独立缺陷叠加**，与"输出顺序"无关——后端本来就是顺序的。

### 3.0 先排除"顺序"这个假设

探针实测：同一个 plan 内 `project_agent_event` 串行执行；`_flush_agent_text` 在
`agent/function_call` 分支**之前**触发（[`runtime_events.py:199-200`](../src/athena/research/runtime_events.py#L199-L200)）；
`_append_log` 在 `_publish` 的第一个 await 之前同步完成，所以落盘顺序 = seq 顺序。

一次真实 agent turn 的两路输出：

```
LIVE:      seq=1,2,3 (text_delta)  [seq=4 flush 只落盘不推送]  seq=5 tool_call  seq=6 tool_out  seq=7,8 delta
PERSISTED: seq=4 "Let me inspect the data."  seq=5 tool_call  seq=6 tool_out  seq=9 "Found train.csv."
```

**结论：不需要加锁。** 问题在消息身份与去重。

### 3.1 心跳超时会把整个 journal 从头重放（`fix/research-journal-replay`）

#### 根因

[`agent_turn_common.py:20`](../src/athena/research/agent_turn_common.py#L20)
`TURN_HEARTBEAT_SECONDS = 300`。`wait_run_with_heartbeat` 在一个 `while True` 里
用 `asyncio.wait_for(waiter, timeout=min(TURN_HEARTBEAT_SECONDS, remaining))` 等待，
超时后 `finally` 取消 waiter，下一轮**重新**调 `wait_run_events(agents, run_id, publish)`。

而 [`plans.py:30-37`](../src/athena/research/supervisor/plans.py#L30-L37) 的
`forward_run_events` 用 `agents.run_events(run_id, after_sequence=0)`——
**从 journal 的第 0 条开始重放**。

SEARCH / 训练类 turn 超过 5 分钟是常态，所以这条几乎必然触发：
整段 agent 文本与工具调用被重新投影一遍，带着新的 seq 推给前端。

#### 复现

让一个 agent turn 跑超过 300 秒（真机训练即可），观察 GUI 对话区：
整段轨迹重复出现，且第一条重放文本会被粘到上一条消息尾部（见 3.2 的实测输出）。

#### 修复方案

`forward_run_events` 记录已转发的 `event.sequence`，重入时用
`after_sequence=last_forwarded` 而不是 0。落点有两种：

- 让 `wait_run_with_heartbeat` 持有游标并传给 `wait_run_events`（改动面小，但要多传一个参数）；
- 或把游标封进一个小的 forwarder 对象，跨心跳复用（更干净，但要动 `plans.py` 的签名）。

推荐前者：`wait_run_events(agents, run_id, publish, after_sequence=cursor)`，
`forward_run_events` 每转发一条就回写游标。

#### 涉及文件

- `src/athena/research/supervisor/plans.py`
- `src/athena/research/agent_turn_common.py`

#### 验收

- 单测：假 `agents.run_events` 记录每次调用的 `after_sequence`；模拟一次心跳超时后
  重入，断言第二次不是 0，且 `publish` 不会收到重复事件。

### 3.2 Agent 文本没有消息身份，前端靠猜合并（`fix/gui-message-identity`）

#### 根因

后端对 agent 文本发两种形状**完全一样**的事件：

- 实时：逐 delta，`persist=False`（[`runtime_events.py:210-215`](../src/athena/research/runtime_events.py#L210-L215)）；
- 落盘：`_flush_agent_text` 合并后的整条（[`runtime_events.py:168`](../src/athena/research/runtime_events.py#L168)）。

两者都是 `source=agent, channel=text, plan=P`，且 seq 不同源。前端拿不到
"这是同一条消息的增量"还是"这是一条完整的新消息"的区分，只能靠
[`usePipeline.ts:197`](../athena-gui/src/hooks/usePipeline.ts#L197) 的启发式：
「上一条是不是同 plan 的 agent 文本，是就 `content += text`」。

于是一条独立的 agent 消息会被静默塞进上一条的尾巴里，**它自己那条就消失了**。

叠加第二个问题：[`MessageList.tsx:127`](../athena-gui/src/components/conversation/MessageList.tsx#L127)
用 `key={msg.id}`，而 `id = out-${seq}`；挂载路径
[`usePipeline.ts:332`](../athena-gui/src/hooks/usePipeline.ts#L332) 用
`restoreRecords(records, /*resetMessages*/ false)` 把回放**追加**在已到达的 live 事件之上
→ 同 key 出现两次 → React 只渲染其中一个。

#### 证据（已实测）

把上面那两路真实事件喂进前端 reducer（vitest + `renderHook(usePipeline)`）：

```
--- MOUNT REPLAY OVER LIVE (7) ---
  id=out-1  "Let me inspect the data."
  id=out-5  shell_command({"command": "ls"})
  id=out-6  train.csv
  id=out-7  "Found train.csv.Let me inspect the data."   ← 回放的整条被吞进上一条尾巴
  id=out-5  shell_command({"command": "ls"})              ← 重复 key
  id=out-6  train.csv                                     ← 重复 key
  id=out-9  "Found train.csv."
duplicate react keys: [ 'out-5', 'out-6' ]
```

`out-4`（"Let me inspect the data." 那条独立消息）在结果里**根本不存在**——
它被合并进了 `out-7`。这就是"Agent 消息被撤删"的直接机制。

#### 触发场景

- 启动或切工作区时会话正在跑（被 `_resume_running_session` 自动续跑）→
  live 事件与 mount 回放叠加。
- 3.1 的心跳重放 → 同一 turn 的文本被重复投影，同样触发粘连。

#### 修复方案

**治本**：给 `OutputEvent` 加 `message_id`——同一条 agent 消息的所有 delta 与
最终落盘记录共用一个 id（`_agent_buffers` 里已经按 plan 维护了缓冲，在建立缓冲时
分配一个 `new_id("msg")` 即可）。前端改成按 `message_id` upsert，React key 也用它。
这样 (a) 合并不再靠猜，(b) live/replay 天然去重。

**若暂时不想动契约**，最小修复两条一起上：

- mount 路径也用 `restoreRecords(records, true)`，或先按 seq 去重再合并；
- delta 事件带 `streaming: true`、落盘的整条带 `streaming: false`，
  前端只对前者做 `content +=`。

注意 `OutputEvent` 是 `extra="forbid"` 的 pydantic 模型，加字段要同步
`athena-rust/` 的契约 fixture（`scripts/export_rust_contract_fixtures.py`）
和 `athena-gui/src/lib/tauri-bridge.ts` 的 `SessionRecord`。

#### 涉及文件

- `src/athena/research/supervisor/events.py`（`OutputEvent`、`EventProjector`）
- `src/athena/research/runtime_events.py`（`_agent_buffers`、`_flush_agent_text`）
- `athena-gui/src/hooks/usePipeline.ts`、`athena-gui/src/components/conversation/MessageList.tsx`
- `athena-gui/src/lib/tauri-bridge.ts`

#### 验收

- 前端单测：live 流 + 落盘回放两路喂进 `usePipeline`，断言消息条数、内容与
  React key 唯一性；断言不存在"整条被吞进上一条尾巴"的内容。
- 后端单测：一次 turn 的所有 delta 与最终落盘记录共用同一个 `message_id`；
  两次连续 turn 的 `message_id` 不同。

---

## 四、会话删不掉 / 空工作区凭空占一行"新会话"

### 4.1 `default` 会话不可删

前端两处硬编码特判：

- [`usePipeline.ts:606`](../athena-gui/src/hooks/usePipeline.ts#L606)：
  `deleteSession` 对 `id === "default"` 直接 `return`。
- [`ContextSidebar.tsx:187`](../athena-gui/src/components/shell/ContextSidebar.tsx#L187)：
  用 `session.id !== "default"` 隐藏删除按钮。

后端 [`handler.py:246-249`](../src/gui_gateway/handler.py#L246-L249) 的
`session_delete("default")` 也只 `unlink` 了
`.athena/logs/sessions/default.jsonl`，**不动 `state.json` / `resume.json` /
`research_tree.json` / artifacts**。就算前端放开，也只是清空对话，
会话本体和第一节那份脏 `RUNNING` 都还在。

### 4.2 空工作区也会列出一行

[`handler.py:222-234`](../src/gui_gateway/handler.py#L222-L234) 的 `_session_ids()`
无条件把 `"default"` 放在第一位，与目录是否存在无关。实测：

```
empty workspace sessions -> ['default']
after one named session -> ['default', 's-1']
```

侧栏因此对**每个**工作区（含 `recentRoots` 里的其它工作区）都渲染一行；
标题在 localStorage 里查不到就回退成 `"新会话"`，就是看到的"空白占用"。

### 4.3 顺带挖出的三个相关问题

- **挂载永远跳回 default**：`_session_ids` 的 docstring 写"最近修改在前"，但
  `default` 是硬编码第一位，所以
  [`usePipeline.ts:322`](../athena-gui/src/hooks/usePipeline.ts#L322)
  的 `const active = list.length ? list[0] : "default"` **恒等于 `"default"`**。
  每次启动或切工作区都跳回默认会话，命名会话不会被恢复。
- **空白会话自动清理有误删风险**：`switchSession` / `newSession` 会在切走时静默
  `sessionDelete(previousId)`，判定用的 `currentSessionBlank` 依赖
  `runStarted.current`（切会话时被重置为 false）和 `viewModel.status`
  （见第一节，可能是错的）。
- **Windows 句柄占用**：`_rmtree_when_released` 重试 10 次（约 3 秒）后仍可能因
  artifacts / git 句柄未释放而抛 `PermissionError`，前端只能看到"删除会话失败"。
  这是"有些会话删不掉"的另一个来源。

### 修复方案

1. **让 `default` 可删**，语义定义为"重置默认会话"：删除前先 `_swap_runtime` 释放句柄，
   再清 `logs/sessions/`、`state.json`、`resume.json`、`research_tree.json`
   （保留工作区目录本体与 `workspaces/`）。前端去掉两处 `"default"` 特判。
2. **空工作区返回空列表**：`_session_ids` 仅在 `.athena/state.json` 或
   `.athena/logs/sessions/default.jsonl` 存在时才列出 `default`；
   前端在 `sessions.length === 0` 时渲染"暂无会话"而不是一行空白。
3. **修正排序 / 恢复上次会话**：让 `default` 也按 mtime 参与排序，使 docstring
   名副其实；或者更直接——把 last-active session id 跟 `active_project_root` 一起存进
   `GuiState`（`src/gui_gateway/state_store.py`）持久化，挂载时用它而不是 `list[0]`。
4. **收紧自动清理**：`currentSessionBlank` 只在"该会话目录下不存在 transcript 且
   state.json 不存在"时成立（改成后端判定，前端不猜）。
5. **删除失败要说清原因**（见第五节）。

### 涉及文件

- `src/gui_gateway/handler.py`、`src/gui_gateway/state_store.py`
- `athena-gui/src/hooks/usePipeline.ts`、`athena-gui/src/components/shell/ContextSidebar.tsx`

### 验收

- 单测：空目录 `_session_ids()` 返回 `[]`；只有 transcript 时返回 `["default"]`。
- 单测：`session_delete("default")` 之后 `state.json` / `research_tree.json` 不再存在。
- 单测：`_session_ids` 按 mtime 排序，`default` 不再恒占首位。
- 前端单测：`sessions` 为空时渲染空态而不是一行"新会话"。

---

## 五、跨问题：错误信息被吞掉

[`transport.py:76-86`](../src/gui_gateway/transport.py#L76-L86) 的异常分支把所有
异常统一变成 `"request failed"`：

```python
error=rpc_error(map_exception_to_error_code(exc), "request failed")
```

上面四个问题在现场都因此没法诊断——删除失败时用户只看到"删除会话失败：request failed"，
不知道是句柄占用、路径非法还是 runtime 已关闭。事件文本已经过
`redact()` 处理，建议至少把 `str(exc)` 带回去（必要时也过一遍 `redact`）。

改这一处会影响所有 RPC 的错误 payload，注意同步
`tests/test_gui_protocol_contract.py` 与 `athena-gui/src/lib/errors.ts`。

---

## 附：本文结论的验证状态

| 结论 | 验证方式 |
|---|---|
| `aclose()` 后 `state.json` 仍为 `RUNNING` | 实测（构造 runtime → `save` → `aclose` → `load`） |
| 空工作区 `_session_ids()` 返回 `['default']` | 实测 |
| live / 落盘两路 seq 不同源 | 实测（`RuntimeEvents` 探针，假 store + 假 supervisor） |
| 回放叠加 live 导致消息被吞 + 重复 React key | 实测（vitest `renderHook(usePipeline)`） |
| `forward_run_events` 心跳重入从 `after_sequence=0` 重放 | 代码路径推导（`agent_turn_common.py` + `plans.py`） |
| 单 runtime 单例导致会话不能并行 | 代码路径推导（`handler.py` + `transport.py`） |
| `default` 不可删的两处前端特判 | 代码阅读 |

探针脚本是一次性的，未入库；上表前四条可按各节"验收"里的单测形式固化。

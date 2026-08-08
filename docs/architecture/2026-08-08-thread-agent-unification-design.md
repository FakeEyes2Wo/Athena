# Thread 归一的 Agent 运行时设计(unify agent runtime onto app_server)

Status: approved (2026-08-08)
Supersedes: `docs/architecture/2026-08-08-agent-runtime-framework-design.md`(AgentKernel 设计,本文将其退役)

## 背景

仓库存在两套重复的 Agent 执行运行时:

- `athena.core.agent_kernel.AgentKernel` — Agent 树编排:单一命令序列器、`AgentGraphStore`(agent/run/journal/wait/outbox 持久化到 `project.json`)、`AgentScheduler`(全局并发上限)、mailbox/wait/human-wait。
- `athena.app_server.ThreadRuntime` — Codex 风格 Thread/Turn 执行:每 Thread 串行、submission+control 双队列、`EventJournal`、Memory 接入(ContextManager/Compactor/RolloutRecorder)、in-process Client/Server 与 WebSocket 桥。

二者执行核心(串行状态机/日志/中断/等待/每执行单元资源)高度重复,互不调用,各有独立消费者(研究流水线走 kernel;GUI 传输与 debate Ideator 走 app_server)。代码注释(`core/agent/models.py`)与提交历史表明迁移方向原本是并入 kernel;本文**反转方向**——以 app_server 的 Thread 模型为统一底座。

## 决策记录

1. **形态**:Thread 即 Agent,kernel 退役。`AgentKernel` 及其 store 删除;每个逻辑 Agent = 一条 Thread(`agent_id == thread_id`)。
2. **持久化**:Codex 风格轻持久化。线程对话经 `RolloutRecorder` JSONL 落盘;重启从 rollout 恢复会话;**活的 wait/human-wait 与 agent 状态不跨重启保留**;`project.json` 仍保存确定性事实(phase/task/budget/tree/memory 等)。
3. **并发**:仅每 Thread 串行(ThreadRuntime 既有保证),无全局并发上限。`AgentScheduler`/`max_active_agents` 删除。
4. **抽象落点**:`AgentControl`/`AgentHandle`/`AgentRun` + 全部 types + `AgentTypeRegistry` + `JsonCodec` 迁入 `athena.core.agent`,删除 `agent_kernel` 包。业务 Agent 契约(`BaseAgent.run(AgentContext) -> AgentOutcome`)不变。

## 目标架构

```
athena.core.agent            # 保留的抽象(新家)
├── models.py                # BaseAgent/AgentContext/AgentOutcome/AgentConfig(不变)
├── runtime.py               # BaseAgent(不变)
├── types.py                 # ← agent_kernel/types.py(AgentStatus/RunStatus/AgentSpec/AgentMessage/
│                            #   AgentEvent/RunSummary/AgentSnapshot/AgentWaitResult/ErrorCode/
│                            #   ReturnWhen/协议/错误契约,原样)
├── control.py               # ← agent_kernel/control.py(AgentControl/AgentHandle/AgentRun,近原样)
├── registry.py              # ← AgentTypeRegistry(原样)
├── codec.py                 # ← JsonCodec(原样)
├── session.py               # RunSession 保留为"门面构造的每 turn 视图"
│                            #   (receive_messages/checkpoint/memory/agent_id/context_ref/kernel)
│                            #   → BaseAgentRunner 主体不动
└── agent_runtime.py         # 【新】AgentRuntime — 门面协调层,实现全部 kernel 实例方法

athena.app_server            # Codex 底座(几乎不动)
├── thread_runtime.py        # ThreadRuntime(+1 个终态回调钩子)
└── thread_manager.py        # RuntimeThreadManager(+可选确定性 rollout 路径)
```

`AgentRuntime` 包装**一个** `RuntimeThreadManager`;每线程挂一条门面记录:

```python
ThreadFacadeRecord:
    agent_id: AgentId        # == thread_id
    agent_type: str
    spec: AgentSpec
    parent_id: AgentId | None
    name: str
    path: AgentPath
    mailbox: deque[AgentMessage]   # send_message 未触发 turn 的未读消息
    rollout_path: Path | None      # 本线程 JSONL 路径(确定性命名,供恢复)
```

## 语义映射(kernel → Thread)

| kernel | Thread 模型 |
|---|---|
| `create_root(type, task, name)` | registry 取 spec → codec.encode_request(task) → `manager.start(agent_id, req_ref)` + `manager.submit(agent_id, req_ref)`;返回 `(agent_id, run_id=turn_id)` |
| `spawn(parent, type, task)` | 同 create_root,门面记录记 `parent_id`(树退化为簿记,无 tree-walk/store) |
| `followup(agent_id, task)` | codec.encode → `manager.submit`(新 turn = 新 run);ThreadRuntime 已拒绝活跃 turn 并发 → 映射 `AgentBusyError` |
| `send_message(agent_id, content, refs, source)` | 追加 `AgentMessage` 到门面 mailbox(内存,不触发 turn) |
| `wait_run(run_id, timeout)` | `manager.wait_turn(agent_id, turn_id)` → `RunSummary` |
| `wait_agent(targets, return_when, timeout)` | `asyncio.wait` 多个 turn future;FIRST_COMPLETED/ALL_COMPLETED;timeout 返回部分 |
| `wait_for(agent_id, target_ids)` | `WaitRegistry`(内存)注册;目标终态回调解析;唤醒投空请求(现有空唤醒处理) |
| `wait_for_human(agent_id, content, refs)` | WaitRegistry 注册 human 等待,返回稳定 `request_id`,结束当前 turn |
| `human_reply(request_id, reply)` | 解析等待:reply 写入 mailbox(source=user) + 空唤醒,返回新 run_id |
| `interrupt(agent_id, reason)` / `cancel_run(run_id)` | `manager.interrupt(thread_id, turn_id, reason)` |
| `close(agent_id, recursive)` | `manager` 关 thread;recursive 沿 parent_id 走子树 |
| `list_agents(path_prefix)` / `agent_status` / `agent_snapshot` / `agent_path` | 由门面记录 + thread.state + WaitRegistry 投影 |
| `run_events` / `session_events(after_sequence)` | `ThreadRuntime.journal.read_from`(turn_id→run_id, thread_id→agent_id, sequence 复用) |
| `pause()` / `resume()` | 门面标志位:暂停时 followup/spawn 报错(见「错误映射与降级」);`start()`/`aclose()` 委托 manager |
| 全局 `AgentScheduler` | 删除 |

### response_ref 兼容

`ProjectRuntime` 现有调用 `json.loads(summary.response_ref)["result_ref"]` 必须不改。门面 runner 适配器把业务返回的 `{"result_ref": outcome.result_ref}` 经 `codec.encode_response` 编码为 JSON 字符串,作为 thread 级 `result_ref` 透传;`wait_run` 直接将其填入 `RunSummary.response_ref`。

## 关键机制

- **mailbox**:门面记录内 `deque[AgentMessage]`;`send_message` 追加;下一 turn 的 runner 经 `RunSession` 视图读取并清空(=checkpoint 语义)。跨重启丢失(已定)。
- **WaitRegistry**(内存,门面持有):`{waiting_agent_id → WaitEntry}`;WaitEntry = 目标 agent_ids 或 human request_id + 解析 future。终态回调(见「app_server 改动面」)触发解析;解析后若无活跃 turn 则投空请求唤醒。空请求时 `BaseAgentRunner` 不生成假 trigger(现有逻辑 §4.4)。
- **runner 适配**:AgentRuntime 为每 agent 构造适配器,实现 `run_with_context(thread, turn, emit, memory, cancel)`(ThreadRuntime 既有双签名兼容):codec.decode_request(turn.request_ref) → 构造 RunSession 视图(mailbox 未读 + memory + agent_id + context_ref + kernel=runtime) → 调现有 `BaseAgentRunner.run(request, *, session, emit)`。BaseAgentRunner 主体不动,只加适配入口。
- **编排工具**:`RunToolProjector.build(agent_type, session)` 中 `session.kernel` 现在指向 AgentRuntime;`_SpawnTool`/`_SendTool`/`_FollowupTool`/`_WaitForTool`/`_WaitForHumanTool` 改调 runtime 方法;静态权限矩阵原样。
- **AgentContext**:`thread`/`turn` 由 thread_models 构造(现状);`memory` = ThreadRuntime 注入的 ContextManager(现状);`messages` = [trigger, *unread mailbox];`tools` = projector.build(agent_type, session)。

## 兼容层注释规范(必须执行)

凡为迁移保留的兼容层,代码中必须以统一前缀注释标注,写明保留的旧契约与清理条件;**禁止出现无标注的兼容分支**。spec 中任何"兼容/迁移期"表述都必须能对应到代码里的 `COMPAT:` 标注,实现与评审按此核验。

| 兼容层 | 保留的旧契约 | 标注位置 | 清理条件 |
|---|---|---|---|
| `response_ref` JSON 信封(`{"result_ref": ...}`) | `ProjectRuntime` 现有 `json.loads(summary.response_ref)["result_ref"]` 调用 | `agent_runtime.py` wait_run / runner 适配器 | ProjectRuntime 改用强类型返回后 |
| `BaseAgentRunner.run_with_context` 适配入口 | kernel runner 协议 `run(request, *, session, emit)` | `base_runner.py` | AgentRunner 协议统一为线程 runner 后 |
| `RunSession` 视图 | `receive_messages()/checkpoint()/memory/agent_id/context_ref/kernel` | `core/agent/session.py` | BaseAgentRunner 不再依赖 session 后 |
| 空唤醒不生成假 trigger | kernel §4.4 wait 唤醒语义 | `base_runner.py` 与 WaitRegistry 唤醒处 | 等待语义内建到 thread 后 |
| `AgentOutcome.next_context_ref` 迁移字段 | ThreadRuntime 旧字段 | `core/agent/models.py` | 旧调用方迁移完成后删除 |
| ThreadRuntime 双签名检测(`run_with_context` vs `run`) | 兼容旧 `agent_runner()` 包装器 | `thread_runtime.py` `_run_turn` | 全部 runner 统一带 context 后 |
| `IdeatorAgent` 确定性 fallback + `run_impl` 注入 | 首版无 LLM 的确定性骨架 | `simple_agents.py` | 真实 Ideator 全量接线后 |

标注格式(代码注释):

```python
# COMPAT: 保留 <旧契约>;清理条件:<条件>。
```

## 持久化与 ProjectRuntime

- `project.json` **去掉 `store` 字段**(不再存 AgentGraphStore),保留确定性事实:`root_supervisor_id`、phase、status、task、eval_specs、sota_ref、validation_ref、report_ref、memory、budget、tree。
- 线程对话走 `RolloutRecorder` JSONL,rollout 路径按 `agent_id` **确定性命名** `{project}/.athena/sessions/{agent_id}.jsonl`(需 app_server 支持注入确定性路径,见「app_server 改动面」)。
- `open()`:读 project.json 事实 → 若存在 supervisor rollout 则 `resume_context_sync(path)` 恢复其 ContextManager 并重建线程,否则新建 → 注册 specs。
- 删除:`_recover`/`_rebuild_sessions`/`store_to_json`/`load_store_json` 及其调用。
- **明确降级**:重启后 wait/human-wait 不恢复(等待中的 Agent 变为新建/重开状态);supervisor 记忆从 JSONL 恢复,活的等待与 in-flight run 不续跑。

## app_server 改动面(最小挂钩,共 2 处)

1. `ThreadRuntime.__init__` 增加可选 `on_turn_terminal: Callable[[str, TurnTerminalState], None] | None = None`,在 `commit_completed`/`commit_failed`/`commit_interrupted` 三处同步调用(回调须为同步、轻量,内部不得 await;门面据此调度异步唤醒任务)。
2. `RuntimeThreadManager._make_runtime` 支持可选确定性 rollout 路径(按 thread_id 命名),否则沿用现状随机路径。debate Ideator / gui_gateway 不改动。

## 删除清单

- `src/athena/core/agent_kernel/` 整个包:`kernel.py`、`session.py`(原实现)、`store.py`、`store_json.py`、`__init__.py`、`control.py`(迁入 core.agent)、`registry.py`(迁入)、`codec.py`(迁入)、`types.py`(迁入)。
- `AgentScheduler` 类与 `max_active_agents` 参数。
- kernel store 持久化(`store_to_json`/`load_store_json`)。
- `core/agent/models.py` 中对 ThreadRuntime 迁移字段的注释说明可清理。

## 错误映射与降级

- 门面把 app_server 异常统一转为 kernel 错误契约:`ClosedError`/`OverloadedError` → `AgentCommandError(CLOSED/OVERLOADED)`;ThreadRuntime 活跃 turn 并发 `RuntimeError` → `AgentBusyError(BUSY)`;未知 thread → `NOT_FOUND`。
- `AgentRun.wait()` 抛 `AgentRunFailed`/`AgentRunInterrupted` 语义不变;响应解码失败仍抛 `AgentRunFailed`(R6)。
- **降级**:`pause()` 后 `followup`/`spawn` 直接报错(原 kernel 语义为排队等待派发;无全局队列,故改报错)。`ProjectRuntime.pause()`/`stop()` 调用不受影响。

## 测试策略

- **迁移到新门面**:`test/unit/agent_kernel/` 下语义类测试迁到 `test/unit/agent/`:test_control、test_kernel(改为门面)、test_waits、test_review_loop、test_memory_restore、test_orchestration、test_base_agent_runner、test_simple_agents、test_data_agent、test_invariants、test_registry、test_types。
- **删除/重写**:test_store、test_store_json(Codex 风格无 graph store);新增门面↔app_server 集成测试(终态回调、mailbox 空唤醒、human_reply、rollout 恢复)。
- 业务 Agent 测试(DataAgent/ReportAgent 等)若只依赖 `AgentContext`,保持不变。

## 变更面清单

| 文件 | 动作 |
|---|---|
| `core/agent/agent_runtime.py` | 新增:AgentRuntime 门面 |
| `core/agent/{types,control,registry,codec,session}.py` | 迁入/保留 |
| `core/agent/__init__.py` | 重导出新面 |
| `core/agent_kernel/*` | 删除 |
| `app_server/thread_runtime.py` | +on_turn_terminal 钩子 |
| `app_server/thread_manager.py` | +确定性 rollout 路径 |
| `agents/base_runner.py` | +run_with_context 适配入口,主体不动 |
| `agents/orchestration.py` | 改绑 AgentRuntime(import/构造) |
| `research/project_runtime.py` | 构造/持久化改造(去 store) |
| `research/runtime.py` | 不变 |
| `agents/ideator/*`, `gui_gateway/*` | 不变 |
| `test/unit/agent_kernel/*` | 迁移/删除 |

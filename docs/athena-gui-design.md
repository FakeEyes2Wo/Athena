# Athena GUI — 完整设计规格 (athena-gui)

> 目标：完成 `athena-gui`（Vite + React + ReactFlow + Tauri 桌面壳 + Python 后端网关），
> 使其具备研究树可视化、假设图可视化与图算法、运行设置、LLM 输入/输出轨迹回放、实验管理，
> 并保留 Python / TypeScript 双后端接口。本文为唯一权威契约，前端、Rust 桥、Python 网关三方以本文为准对齐。

---

## 1. 现状与断点

### 1.1 已有资产（勿破坏，逐项对齐）

| 层 | 位置 | 现状 |
|---|---|---|
| 前端 | `athena-gui/src` | React 壳已存在：`AppShell`、`SessionSidebar`、`ConversationPane`、`RightRail`、`ContextSurface`、`ResearchTreeViz`（ReactFlow）、`MetricChart`、`FileTree`、`DiffViewer`、hooks `usePipeline`/`useEvents` |
| 桥 | `athena-gui/src/lib/tauri-bridge.ts` | `invoke`/`listen` 封装，含 `ResearchTreeData` 等类型 |
| Rust | `athena-gui/src-tauri` | `PythonBridge`（`uv run python -m gui_gateway` 起子进程 + WS 读端口 + RPC `call(method, params)` + 事件广播 `EventNotification{kind,data}`）；命令：`send_message`、`start/pause/resume/stop_search`、`start_validation`、`generate_report`、`tree_get/save/load` |
| Python 网关 | `src/gui_gateway/{__main__,handler,transport}.py` | `GuiRequestHandler.dispatch` 仅实现 `ping/start/message/pause/resume/stop`；`WebSocketTransport` 桥接 `RequestEnvelope`/`ResponseEnvelope` |
| Python 运行时 | `src/athena/research/runtime.py` → `ResearchRuntime` | 唯一门面：`subscribe(emit)` 推 `state`/`output` 事件，`tree`、`state`、`message()`、`start()`、`start_task()` |
| 领域模型 | `src/athena/core/{research_tree,research_models}.py` | `ResearchTree`（hypotheses/experiments/sota）、`Hypothesis`、`Experiment`、`ExperimentPlan`、`EvalResult`、`ComparisonVerdict`、`HypothesisStatus` |
| 状态 | `src/athena/research/supervisor/{state,plans,events}.py` | `ResearchState`（status/phase/search_limit/concurrency/manual_mode/plans/validation/eda_dir）、`PlanState`、`StateEvent`/`OutputEvent` |
| LLM I/O 源 | `src/athena/memory/rollout.py` + `app_server/thread_manager.py` | 确定性 rollout JSONL：`.athena/logs/agents/{agent_id}.jsonl`，每行 `{"seq","ts","msg":[ModelMessage…]}` 或 `{"type":"compaction",…}`，`msg` 为 PydanticAI `ModelMessagesTypeAdapter` 序列化 |
| TS 后端 | `athena_ts/` | 未完成，**本次不动**，仅保留其包边界 |

### 1.2 关键断点（本次要修的核心问题）

**协议失配**：Rust 命令发送 `PARSE_INTENT` / `SEARCH_START` / `SEARCH_PAUSE` / `SEARCH_RESUME` / `SEARCH_STOP` / `VALIDATE_START` / `REPORT_GENERATE` / `tree_get` / `tree_save` / `tree_load`；
而 Python `GuiRequestHandler.dispatch` 只认识 `ping/start/message/pause/resume/stop`。**整条链路目前不工作**。
本文第 3 节定义唯一权威协议，Python 网关、Rust 命令、TS 桥三处同时对齐。

---

## 2. 目标与设计原则

1. **Python 后端优先**：所有图算法、设置、轨迹、实验查询都在 `src/athena/gui/` 实现并测试；前端只做渲染与薄 RPC 封装。
2. **保留双后端接口**：`athena_ts/` 不触碰；协议契约（DTO 字段名、方法名、枚举值）与 Python 解耦，未来 TS 后端只需复刻同一份方法表。
3. **事件驱动 + 快照**：`state`/`output` 事件流用于实时推进；`tree_get`/`state_get` 用于按需拉全量，二者幂等可重放。
4. **算法纯函数化**：图算法输入为 `ResearchTree.to_dict()` 的 dict（或等价模型），输出为纯 JSON，不依赖运行时状态，便于单测与前端镜像。
5. **只读优先、写操作显式**：读操作幂等安全；写操作（transition/set_sota/settings_set/实验删除）显式暴露且带前置校验。
6. **字段级中文注释**：与既有 `research_tree.py` 风格一致。

---

## 3. 权威 GUI 协议（单点事实源）

### 3.1 传输信封（沿用 `app_server/protocol.py`，不改）

- 请求：`RequestEnvelope { request_id: int≥0, method: str, params?: dict }`
- 响应：`ResponseEnvelope { request_id, result?: dict, error?: RpcError }`
- 事件：`EventNotification` 经 `transport` 转成 `{"kind": "...", "data": {...}}` 推给 WS 客户端；Rust `events.rs` 再把 `{kind,data}` 原样 `app.emit(kind, {kind,data})`。

### 3.2 方法总表（method → params → result）

> 命名统一为 `snake_case`。Rust 命令与 TS 桥不得自创方法名，一律引用本节。

#### A. 运行时控制

| method | params | result |
|---|---|---|
| `ping` | — | `{ "pong": true }` |
| `start` | — | `{ "started": true }` |
| `start_task` | `{ "task": str }` | `{ "status": str }` |
| `message` | `{ "text": str }` | `{ "response": str }` |
| `pause` | — | `{ "status": str }` |
| `resume` | — | `{ "status": str }` |
| `stop` | — | `{ "status": str }` |
| `parse_intent` | `{ "message": str }` | `{ "task_type", "data_type", "target_vars", "primary_metric", "direction", "needs_configuration" }` |
| `start_search` | `{ "config": dict }` | `{ "status": str }` |
| `start_validation` | — | `{ "status": str }` |
| `generate_report` | — | `{ "status": str }` |

> `parse_intent`/`start_search`/`start_validation`/`generate_report` 由 `athena.agents.prompt_agent`/`plan_agent` 等现有能力实现；
> 若某能力未接线，返回明确错误而非静默。当前第一版把 `parse_intent` 实现为启发式解析（读 config/任务描述），
> `start_search` 委托 `runtime.start_task(task)` + `runtime.start()`，其余返回 `{"status":"ok","note":"not wired"}`（见 §12 风险）。

#### B. 状态与树

| method | params | result |
|---|---|---|
| `state_get` | — | `StateEvent`（status/phase/plans/search/sota/waiting/manual/pending/validation/eda_dir） |
| `tree_get` | — | `{ "tree": ResearchTreeDict }`（含 version/sota_id/hypotheses/experiments） |
| `tree_save` | — | `{ "saved": true, "path": str }` |
| `tree_load` | — | `{ "loaded": true, "tree": ResearchTreeDict }` |
| `sessions_list` | — | `{ "sessions": [str] }`（当前工作区内所有会话 id，最近修改在前） |
| `session_switch` | `session_id: str` | `{ "records": [SessionRecord] }`（切到该会话并重放其 transcript，实现断点续传） |

#### C. 假设图与算法

| method | params | result |
|---|---|---|
| `hypothesis_graph` | — | `{ "nodes": [HypGraphNode], "edges": [HypGraphEdge] }` |
| `graph_algorithms` | — | `{ "algorithms": [ { "name", "label", "params": [ParamSchema] } ] }` |
| `graph_algorithm` | `{ "name": str, "params": dict }` | 算法特定 result（见 §5.3） |

#### D. 设置

| method | params | result |
|---|---|---|
| `settings_get` | — | `GuiSettings`（见 §6） |
| `settings_set` | `{ "patch": dict }` | `GuiSettings`（更新后全量，含校验错误则抛 `INVALID_ARGUMENT`） |

#### E. LLM I/O 轨迹

| method | params | result |
|---|---|---|
| `traces_list` | — | `{ "traces": [TraceSummary] }` |
| `trace_get` | `{ "agent_id": str }` | `{ "agent_id", "messages": [TraceMessage] }` |

#### F. 实验管理

| method | params | result |
|---|---|---|
| `experiments_list` | `{ "kind"?: str }` | `{ "experiments": [ExperimentDetail] }` |
| `experiment_get` | `{ "experiment_id": str }` | `ExperimentDetail`（含 path/descendants） |
| `experiment_transition` | `{ "experiment_id": str, "status": str, "error"?: str }` | `{ "experiment": ExperimentDetail }` |
| `experiment_set_sota` | `{ "experiment_id": str }` | `{ "sota_id": str }` |

### 3.3 错误语义

- `ValueError` → `INVALID_ARGUMENT(-32602)`，`KeyError` → `NOT_FOUND(-32601)`，`RuntimeError` → `FAILED_PRECONDITION(-32000)`，其余 → `INTERNAL(-32603)`。
- 错误 `message` 仅通用文案（不泄露 traceback/prompt/密钥），沿用 `rpc_error`。

---

## 4. 数据模型（Python 侧 DTO，前端 TS 逐字段镜像）

### 4.1 `ResearchTreeDict`（即 `ResearchTree.to_dict()`，`version=3`）

```jsonc
{
  "version": 3,
  "sota_id": "exp-…" | null,
  "hypotheses": { "<hyp_id>": Hypothesis },
  "experiments": { "<exp_id>": Experiment }
}
```

`Hypothesis`：`statement, intervention, expected_effect, status(∈{PROPOSED,SUPPORTED,REFUTED,REJECTED}), evidence_refs[], id, parent_id|null, supersedes[], priority, order, patience, turn_limit|null, sources[]`。

`Experiment`：`parent_id|null, hypothesis_id, commit, plan{kind,change,rubrics[],run_config_ref,budget{},acceptance_rule}, gitwork{path,branch,base_commit}, status(∈{PENDING,RUNNING,SUCCEEDED,FAILED,CANCELLED}), eval{experiment_id,primary,secondary{},per_sample}|null, verdict{winner,p_value}|null, artifacts{}, error|null`。

### 4.2 假设图节点/边

```jsonc
HypGraphNode = {
  "id": "<hyp_id>",                 // 以 hypothesis.id 为图节点主键（唯一，1:1 对应实验）
  "experiment_id": "<exp_id>",       // 该假设对应的实验 id（可能为 null 若尚未登记实验）
  "statement": str, "status": str, "priority": float, "order": int|null,
  "supersedes": [str], "parent_id": str|null, "sources": [str],
  "primary": float|null,             // 实验 eval.primary（无实验则 null）
  "sota": bool                       // 该实验是否为当前 SOTA
}
HypGraphEdge = {
  "id": str, "source": "<hyp_id>", "target": "<hyp_id>",
  "kind": "lineage" | "supersedes",  // lineage=父实验假设→子实验假设；supersedes=子→被取代者
  "label": str                       // 可选展示文案
}
```

> 关键语义：`Hypothesis.parent_id` 指向**父实验**（非父假设）。因此 lineage 边 =
> 对每个实验 `E`（假设 `H`），若 `E.parent_id=P` 且 `P` 对应假设 `Hp`，则加边 `Hp --lineage--> H`。
> `supersedes` 边 = 对每个 `s ∈ H.supersedes`，加边 `H --supersedes--> s`。

---

## 5. 假设图与算法（核心智力资产，Python 纯函数实现）

模块 `src/athena/gui/graph.py`，全部输入 `ResearchTree`（或 `to_dict()`），输出纯 dict，单测覆盖。

### 5.1 图构建 `build_hypothesis_graph(tree) -> {nodes, edges}`

- 节点：遍历 `tree._hypotheses`（经公开迭代器或 `to_dict()`），映射 `experiment_for_hypothesis(hyp_id)` 求 `experiment_id` 与 `primary`。
- 边：`lineage`（见 §4.2）+ `supersedes`。

### 5.2 算法清单（`graph_algorithms` 返回）

| name | 说明 | params | result |
|---|---|---|---|
| `topological_order` | 按实验谱系 BFS 层序排序假设 | — | `{ "order": [hyp_id] }` |
| `cycle_detect` | 检测 lineage+supersedes 并图是否含环 | — | `{ "acyclic": bool, "cycles": [[hyp_id]] }` |
| `lineage` | 某实验（或其假设）到根的祖先链 | `{ "experiment_id" }` | `{ "path": [exp_id], "hypotheses": [Hypothesis] }` |
| `active_hypotheses` | 选定父实验 + 子假设下的**活跃假设集**（祖先 − 被 supersede 者 + 子） | `{ "experiment_id", "child_hypothesis_id" }` | `{ "active": [Hypothesis], "superseded": [hyp_id] }` |
| `descendants` | 某实验的 BFS 后代 | `{ "experiment_id" }` | `{ "descendants": [exp_id] }` |
| `best_path` | 当前 SOTA 及其祖先假设链 | — | `{ "sota_id", "path": [exp_id], "hypotheses": [Hypothesis] }` |
| `rank_pending` | 按 priority 升序排序 PROPOSED 假设 | — | `{ "ranked": [ {hyp_id, priority, statement} ] }` |
| `supersedes_closure` | supersedes 传递闭包（含间接被取代者） | `{ "hypothesis_id" }` | `{ "hypothesis_id", "closure": [hyp_id] }` |
| `refutation_reachability` | 某假设被证伪时，下游受影响（沿 lineage 传播）的假设 | `{ "hypothesis_id" }` | `{ "affected": [hyp_id] }` |

### 5.3 算法实现要点

- `topological_order`：复用 `ResearchTree` 的 BFS（`root_experiment_ids` + `list_children`），对每层实验取假设；无父实验假设排最前。
- `cycle_detect`：对有向并图（lineage ∪ supersedes）做 DFS 三色标记（白/灰/黑），灰遇灰即环。lineage 本身是树必无环；环只可能由 supersedes 与 lineage 组合造成（正常构造下 supersedes 仅指向祖先，恒无环——此算法是**防御性校验**）。
- `active_hypotheses`：直接委托 `tree.active_hypotheses(exp_id, child)`，返回 `superseded` = 被过滤的祖先 id。
- `supersedes_closure`：对 `H.supersedes` 做 BFS，沿每个被取代者的 `supersedes` 继续，去重收集。
- `refutation_reachability`：从该假设对应实验出发，BFS 其 `list_descendants` 并映射回假设 id。
- `rank_pending`：`tree.pending_hypotheses()` 按 `priority` 升序（小 = 高优先）。

---

## 6. 设置设计

模块 `src/athena/gui/settings.py`。

```jsonc
GuiSettings = {
  "project_root": str,           // 只读
  "model": str|null,             // 已注册 provider 的模型名
  "concurrency": int≥1,
  "search_limit": int≥0,
  "direction": "maximize"|"minimize",
  "tolerance": float≥0,
  "auto_validate": bool,
  "manual_mode": bool,           // 映射 state.manual_mode
  "phase": str,                  // 只读（PREPARE/SEARCH/VALIDATE/COMPLETED）
  "status": str                  // 只读
}
```

- `settings_get` 从 `runtime.state` + `runtime` 构造参数读取；`phase/status` 只读。
- `settings_set(patch)`：白名单字段（`concurrency/search_limit/direction/tolerance/auto_validate/manual_mode`）。
  - `manual_mode` → 走 `runtime.message("/manual"|"/auto")`。
  - `search_limit`/`concurrency` → 改 `runtime.state`（`ResearchState` 可变字段）并落盘。
  - `direction`/`tolerance` 为运行时构造参数，改后仅影响**后续** plan（记录为“延迟生效”）。
  - 非法值抛 `ValueError`。
- 前端 `SettingsPanel`：表单 + 保存按钮 + 只读字段灰显 + 校验错误内联提示。

---

## 7. LLM I/O 轨迹设计

模块 `src/athena/gui/traces.py`，读 `.athena/logs/agents/{agent_id}.jsonl`（确定性 rollout）。

- `traces_list`：扫描 `rollout_dir`，每个 `.jsonl` 文件 → `TraceSummary { agent_id, file, size_bytes, messages, updated_at }`。
- `trace_get(agent_id)`：逐行读 JSONL，把 `msg`（PydanticAI 消息列表）归一为前端友好结构：

```jsonc
TraceMessage = {
  "role": "system"|"user"|"assistant"|"tool",   // 由 ModelRequest/ModelResponse 各 part 归并
  "parts": [ { "kind": "text"|"tool_call"|"tool_return"|"system"|"image"|"args_json", "content": str } ],
  "seq": int, "ts": str
}
```

- 归一化规则：`ModelRequest.parts` 中 `SystemPromptPart→system`、`UserPromptPart→user`、`ToolReturnPart→tool`、`RetryPromptPart→user`；
  `ModelResponse.parts` 中 `TextPart→assistant/text`、`ToolCallPart→assistant/tool_call`（含 `tool_name`+`args_json`）。
- 多 part 一条消息拆成多个 `TraceMessage`（保持时间序），`seq` 取行号。
- 前端 `LLMIOPanel`：左侧 agent 列表，右侧时间线渲染（system/user/assistant 着色 + tool_call 折叠 JSON + tool_return 折叠）。
- **脱敏**：复用 `events.redact` 对 text 做密钥掩码后再回显。

---

## 8. 实验管理设计

模块 `src/athena/gui/experiments.py`。

- `experiments_list(kind?)` → 每个实验展开为 `ExperimentDetail { experiment: Experiment, hypothesis: Hypothesis, sota: bool }`，按 `order` 排序。
- `experiment_get(id)` → 附加 `path`（祖先链）与 `descendants`。
- `experiment_transition(id, status, error?)` → 委托 `tree.transition_experiment`（受 `_ALLOWED_TRANSITIONS` 约束），落盘 `tree_save`。
- `experiment_set_sota(id)` → 委托 `tree.set_sota`（仅 baseline/search 且 SUCCEEDED），落盘。
- **不提供删除**（`ResearchTree` 无删除语义，父子索引一旦建不可撤）；改为提供 `transition` 的 `CANCELLED` 终态。

前端 `ExperimentManager`：表格（状态徽章 + primary + winner + hypothesis 摘要）+ 行详情抽屉 + 状态推进下拉 + “设为 SOTA”按钮（仅合法时可用）。

---

## 9. 文件与组件映射

### 9.1 Python（本次新增/修改，全部在 `src/`）

```
src/athena/gui/__init__.py        # 导出 GuiService
src/athena/gui/graph.py           # §5 图构建 + 全部算法（纯函数）
src/athena/gui/settings.py        # §6 GuiSettings + get/set
src/athena/gui/traces.py          # §7 rollout 读取 + 归一化
src/athena/gui/experiments.py     # §8 实验查询 + 管理
src/athena/gui/service.py         # GuiService：组合 runtime + 上述模块，暴露全部只读/控制方法
src/gui_gateway/handler.py        # 重写 dispatch：路由到 GuiService（§3 全方法）
src/athena/gui/test_graph.py      # 图算法单测
src/athena/gui/test_settings.py   # 设置单测
```

### 9.2 Rust（`athena-gui/src-tauri/src/`）

```
commands/mod.rs      # 新增 mod settings; mod traces; mod graph; mod experiments;
commands/research.rs # 保留 tree_get/save/load，方法名统一
commands/chat.rs     # send_message → parse_intent
commands/search.rs   # start/pause/resume/stop_search → start_search/pause_search/…（方法名统一）
commands/settings.rs # settings_get / settings_set
commands/traces.rs   # traces_list / trace_get
commands/graph.rs    # hypothesis_graph / graph_algorithms / graph_algorithm
commands/experiments.rs # experiments_list/get/transition/set_sota
lib.rs               # 注册全部新命令
```

### 9.3 前端（`athena-gui/src/`）

```
lib/tauri-bridge.ts         # 新增 RPC 封装 + 类型（HypGraphNode/Edge、GuiSettings、TraceSummary/Message、ExperimentDetail…）
types/ui.ts                 # CONTEXT_PANELS 增 hypothesis-graph / algorithms / settings / llm-io / experiments
components/HypothesisGraphViz.tsx   # 假设图（lineage+supersedes 双色边 + 图例 + 布局）
components/AlgorithmsPanel.tsx      # 算法下拉 + 参数 + 结果 JSON/表格
components/SettingsPanel.tsx        # 设置表单
components/LLMIOPanel.tsx           # 轨迹回放
components/ExperimentManager.tsx    # 实验表 + 详情 + 状态推进
components/context/ContextSurface.tsx  # 接线 5 个新面板
components/right-rail/RightRail.tsx    # 新增“假设图”“设置”入口卡
```

---

## 10. 实施阶段（依赖序）

1. **P0 Python 契约**：`graph.py`（算法，先写 + 单测绿）→ `settings/traces/experiments` → `service.py` → 重写 `handler.py` → 全量单测。
2. **P1 Rust 对齐**：改 6 个命令文件 + `lib.rs`，方法名与 §3 一致。
3. **P2 前端桥 + 类型**：`tauri-bridge.ts` + `types/ui.ts`。
4. **P3 前端面板**：`HypothesisGraphViz` → `AlgorithmsPanel` → `SettingsPanel` → `LLMIOPanel` → `ExperimentManager` → 接线 `ContextSurface`/`RightRail`。
5. **P4 联调**：`uv run python -m gui_gateway`（test_mode 下不打印端口）+ `cargo check` + `vitest`。

---

## 11. 测试策略

- **Python**：`graph.py` 每个算法用 `ResearchTree` 手工构造（含 supersedes、多分支、SOTA、REFUTED）断言精确结果；`settings` 用假 runtime 校验白名单与错误；`traces` 用 fixture JSONL 断言归一化；`experiments` 校验 transition 合法性。
- **Rust**：`cargo check` + 手动核对方法名与 `handler.py` 一一对应。
- **前端**：`vitest` 覆盖 `buildGraph`/`layout` 纯函数与 `applyPipelineEvent` reducer；面板渲染用 `render.tsx` 冒烟。
- **契约一致性**：写一个 `tests/test_gui_protocol_contract.py`，断言 `handler.dispatch` 支持的方法集合 == Rust `lib.rs` 注册的命令所映射的方法集合（从常量表读取）。

---

## 12. 风险与待决

1. `parse_intent` 无现成 LLM 意图解析器：第一版用启发式（关键词/配置），返回 `needs_configuration` 引导用户填设置；后续接 `prompt_agent`。
2. `start_search`/`start_validation`/`generate_report` 依赖现有 supervisor 阶段机；未接线处返回明确 `note`，不伪装成功。
3. `settings_set` 中 `direction/tolerance` 为构造期参数，改动不回溯已生成 plan——在 UI 标注“延迟生效”。
4. `hypothesis_graph` 依赖 `ResearchTree` 内部字典：经 `to_dict()`/公开方法访问，不触碰私有字段，保证未来重构兼容。
5. 假设无实验登记（PROPOSED 且未建实验）时 `experiment_id=null`，节点仍渲染，前端降级显示“未安排实验”。
6. 子代理模型：本环境 `Agent` 工具 `model` 枚举仅 `sonnet/opus/haiku/fable`，无法指定 “deepseek flash 1m”；子代理将继承会话模型（deepseek-v4-pro）。已在编排中采用默认继承。

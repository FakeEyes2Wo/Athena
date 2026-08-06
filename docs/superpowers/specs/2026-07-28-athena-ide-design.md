# Athena IDE 设计规范

> 日期：2026-07-28
> 状态：设计完成，待实施
> 关联文档：`docs/superpowers/specs/2026-07-27-athena-ai4ml-design.md`（AI4ML 管线）、`docs/superpowers/specs/2026-07-25-athena-rust-design.md`（Rust 迁移）

Athena IDE 是一个基于 Tauri 的桌面 GUI 应用，为 AI4ML 管线提供完整的对话式交互 + 可视化监控界面。

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    Tauri Desktop App                     │
│  ┌─────────┐ ┌──────┐ ┌──────────┐ ┌────────────────┐  │
│  │ Chat    │ │ File │ │ Diff     │ │ Experiment     │  │
│  │ Panel   │ │ Tree │ │ Viewer   │ │ Dashboard      │  │
│  └─────────┘ └──────┘ └──────────┘ └────────────────┘  │
│  ┌──────────────────────────────────────────────────┐   │
│  │         ResearchTree Viz + Metric Charts          │   │
│  └──────────────────────────────────────────────────┘   │
│                         │                               │
│              ┌──────────┴──────────┐                    │
│              │   Rust Core         │                    │
│              │   PythonBridge      │                    │
│              │   (process + ws)    │                    │
│              └─────────────────────┘                    │
└─────────────────────────────────────────────────────────┘
                          │
              WebSocket (127.0.0.1:{port})
                          │
┌─────────────────────────────────────────────────────────┐
│              Python Backend (athena-ai4s)               │
│  src/athena/ide/                                       │
│  ├── __main__.py     (WS server + 端口输出)             │
│  ├── transport.py    (WS ↔ 协议层)                     │
│  ├── handler.py      (IDE 专用方法)                    │
│  └── stream.py       (流式事件发射)                    │
│                                                         │
│  复用现有模块：                                         │
│  ├── app_server/     (MessageProcessor + ThreadManager) │
│  ├── workflows/      (PREPARE → SEARCH → VALIDATE)     │
│  └── core/           (schemas, ranking, evaluation...)  │
└─────────────────────────────────────────────────────────┘
```

---

## 2. 通信协议

Tauri Rust Core 在应用启动时程序化 `spawn` Python 后端进程，之后所有通信走 WebSocket。

### 启动序列

1. Tauri 用 `std::process::Command` spawn `python -m athena.ide`
2. Python 启动 WebSocket server（`127.0.0.1:0`，随机端口）
3. Python 将端口号写入 stdout 第一行
4. Tauri 读取端口号，连接到 `ws://127.0.0.1:{port}`
5. 之后所有请求/响应/事件走 WebSocket

### 消息格式

复用现有 `app_server/protocol.py` 的 `RequestEnvelope` / `ResponseEnvelope` / `EventNotification`，传输格式为 JSON：

```json
{"request_id": 1, "method": "SEARCH_START", "params": {...}}
{"request_id": 1, "result": {"run_id": "r1"}, "error": null}
{"subscription_id": "s1", "kind": "experiment/completed", "data": {...}}
```

### Tauri 侧 Rust 接口

```rust
struct PythonBridge {
    ws: WebSocket,
    pending: HashMap<u64, oneshot::Sender<Value>>,
    events: broadcast::Sender<EventEnvelope>,
}

impl PythonBridge {
    async fn start() -> Self;                          // spawn + connect
    async fn call(&mut self, method: &str, params: Value) -> Value;
    fn subscribe(&self) -> broadcast::Receiver<EventEnvelope>;
}
```

暴露给前端的 Tauri Command 全部是对 `bridge.call()` 的薄包装。

---

## 3. 面板设计

### Chat Panel（左侧）

流式对话面板。用户输入自然语言 → LLM 解析意图 → 自动填充参数 → 用户确认 → 启动管线。

- 流式 LLM 输出（逐 token 推送）
- 参数确认卡片（内联按钮：确认 / 修改 / 取消）
- 实验进度摘要（内联消息，不过度展开）

### Experiment Dashboard（右侧上）

实时状态面板，数据来自事件流：

```
Phase: SEARCH | Budget: 7/10 | SOTA: f1=0.78 | Streak: 2/5

┌─────────────────────────────────────┐
│  Experiment Log                     │
│  #1 Baseline       → f1=0.72  SUPP  │
│  #2 StandardScaler → f1=0.75↑ SUPP  │
│  #3 FeatureEng     → f1=0.74↓ REFU  │
│  #4 GBDT           → f1=0.78↑ SUPP  │
│  #5 Dropout        → Running...     │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│  Hypothesis Queue                   │
│  1. Dropout(0.5)    ELO:1520  UCB:2.1│
│  2. LR Schedule     ELO:1480  UCB:1.8│
│  3. More Epochs     ELO:1450  UCB:1.5│
└─────────────────────────────────────┘
```

### File Tree + Diff（中右，Tab 切换）

- 点击实验 → 加载该 worktree 的文件树
- 选中文件 → Monaco Editor 显示 diff（vs parent commit）
- 只读模式（实验代码不可手动修改）

### ResearchTree Viz + Metric Charts（底部，可折叠）

- DAG 图：节点 = 实验，颜色 = 状态（绿=SOTA，灰=REFUTED，蓝=RUNNING）
- 点击节点 → 展开实验详情
- Metrics 图表：折线图显示主指标变化趋势，SOTA 标记

---

## 4. Python IDE Backend（`src/athena/ide/`）

### 模块结构

```
src/athena/ide/
├── __main__.py       # 入口：启动 WS server
├── transport.py      # WebSocketTransport
├── handler.py        # IDEHandler
└── stream.py         # StreamEmitter
```

### 复用现有模块（零修改）

- `app_server/protocol.py` — RequestEnvelope, ResponseEnvelope, EventNotification
- `app_server/server.py` — MessageProcessor
- `app_server/thread_manager.py` — RuntimeThreadManager
- `app_server/execution.py` — ExecutionAdapter
- `workflows/` — 全部管线模块

### IDEHandler 方法

| 方法 | 描述 |
|------|------|
| `PARSE_INTENT` | 自然语言 → TaskMetaData + DataProfile 预览 |
| `TASK_CONFIGURE` | 确认/修改任务参数 |
| `SEARCH_START` | 启动 SearchLoop，流式推送实验事件 |
| `SEARCH_PAUSE` | 当前实验完成后暂停 |
| `SEARCH_RESUME` | 恢复循环 |
| `SEARCH_STOP` | 优雅终止，保留已产生结果 |
| `VALIDATE_START` | 消融 + 终测 |
| `REPORT_GENERATE` | 生成 Markdown 报告 |

### StreamEmitter — 流式事件类型

| event kind | 渲染目标 | 数据 |
|------------|---------|------|
| `chat/token` | Chat Panel | 流式 LLM token |
| `chat/intent_parsed` | Chat Panel | TaskMetaData 预览 |
| `experiment/started` | Dashboard | experiment_id, hypothesis |
| `experiment/completed` | Dashboard | EvalResult, ComparisonVerdict |
| `experiment/failed` | Dashboard | error |
| `phase/change` | Dashboard | phase name |
| `budget/update` | Dashboard | BudgetSnapshot |
| `tree/update` | ResearchTree Viz | node id, status, parent_id |

---

## 5. Tauri App 结构

### Rust Core

```
src-tauri/src/
├── main.rs              # Tauri 入口 + setup
├── python/
│   ├── mod.rs
│   ├── bridge.rs        # PythonBridge (spawn + ws + call + subscribe)
│   └── types.rs         # 协议类型
├── commands/
│   ├── mod.rs
│   ├── chat.rs          # send_message, parse_intent
│   ├── search.rs        # start, pause, resume, stop
│   └── validate.rs      # start_validation, generate_report
└── events.rs            # 事件订阅 → Tauri emit to frontend
```

关键 Tauri Command：

```rust
#[tauri::command] async fn send_message(msg: String) -> Result<Value, String> { bridge.call("PARSE_INTENT", ...) }
#[tauri::command] async fn start_search(config: Value) -> Result<Value, String> { bridge.call("SEARCH_START", ...) }
#[tauri::command] async fn pause_search() -> Result<Value, String> { bridge.call("SEARCH_PAUSE", ...) }
// ...
```

### 前端

```
src/
├── App.tsx
├── components/
│   ├── ChatPanel.tsx          # 对话面板
│   ├── ExperimentDashboard.tsx # 实验仪表盘
│   ├── FileTree.tsx           # 文件树
│   ├── DiffViewer.tsx         # Diff 对比
│   ├── ResearchTreeViz.tsx    # 研究树 DAG
│   └── MetricChart.tsx        # 指标趋势图
├── hooks/
│   ├── useEvents.ts           # listen() 事件流
│   └── usePipeline.ts         # 管线状态
└── lib/
    └── tauri-bridge.ts        # invoke() + event 类型
```

**前端技术栈**
- React 18 + TypeScript
- `@tauri-apps/api` — `invoke()` + `listen()`
- Monaco Editor — Diff 面板
- ReactFlow — ResearchTree DAG 可视化
- Recharts — 指标趋势图

---

## 6. 验收标准

| # | 场景 | 预期 |
|---|------|------|
| 1 | Tauri 启动 → spawn Python → WS 连接 | PythonBridge.start() 返回，subscribe 可用 |
| 2 | 输入自然语言 "分析这个 CSV，跑分类" | PARSE_INTENT → LLM 返回 TaskMetaData 预览 |
| 3 | 用户点击确认 → 启动搜索 | SearchLoop 运行，流式推送实验事件 |
| 4 | 实验完成 → Dashboard 更新 | 实验日志行、指标趋势图、ResearchTree 节点实时更新 |
| 5 | 搜索期间点暂停 | 当前实验完成后停止，再点继续可恢复 |
| 6 | 搜索完成 → 消融 + 报告 | 消融结果、终测结果、完整 Markdown 报告 |
| 7 | 点击实验节点 → 查看 Diff | Monaco Editor 显示 vs parent commit 的 diff |
| 8 | 关闭窗口 | Python 后端进程优雅终止 |

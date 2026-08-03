# Athena TUI 设计

参考对象：`beilingcc/claude-code`（Claude Code 的 npm source map 快照，React + Ink，
约 140 个 `src/components/*.tsx`）。本文说明**借鉴了什么**、**因为 Athena 的哪些特点而改了什么**，
以及最终的模块划分与数据流。

## 1. 从 Claude Code 借鉴的三件事

| 借鉴点 | Claude Code 的做法 | Athena 保留的原因 |
| --- | --- | --- |
| **scrollback 原生的转录区** | `<Static>` 提交历史消息，只有底部一小块是动态重绘区 | 终端自带的滚动、选中、复制全部可用；不抢 alt-screen，`Ctrl+C` 退出后记录还在 |
| **底部常驻编排区** | 流式消息 + 状态行 + 输入框 + 补全菜单同属一个动态区 | 正在进行的内容可以原地刷新，定稿后再落入 scrollback |
| **斜杠命令 / 审批 / 中断三件套** | `/commands`、permission dialog、`Esc` 中断 | 这三者是 Agent TUI 的最小交互闭环 |

## 2. 因为 Athena 而做的五处改动

### 2.1 TUI 是纯协议客户端，不 import Agent

Claude Code 的 Ink 组件直接读 `QueryEngine` 的 React state。Athena 已经有
`app_server` 这一层 Client/Server 协议（`initialize` / `thread/start` / `turn/start` /
`thread/subscribe` / `turn/interrupt` / `thread/fork` / `server/shutdown`），
所以 TUI **只依赖 `AthenaClient` 和 `EventNotification`**，不 import 任何 agent、tool、workflow。

```
athena.tui  ──►  AthenaClient  ──►  Transport(进程内队列)  ──►  MessageProcessor
                      ▲                                              │
                      └────────── EventNotification / ServerRequest ──┘
```

好处：换 runner（demo agent / paper_scout / 未来的 Scheduler）不动 TUI 一行代码；
将来 `Transport` 换成 stdio 或 WebSocket，TUI 也只换构造函数。

### 2.2 渲染栈：prompt_toolkit（底部区） + rich（转录区）

Ink 在 Python 没有对等物。拆成两半：

- **rich** 负责把事件渲染成 ANSI 字符串（Markdown、语法高亮、表格、面板）。
- **prompt_toolkit** 的**非全屏 `Application`** 负责底部动态区，并通过
  `patch_stdout(raw=True)` 把 rich 的输出安全地打到动态区**上方**，落进真实 scrollback。

两者都已在 `.venv` 中（`pydantic-ai` / `langchain` 的传递依赖），本次显式声明为
`tui` 依赖组，不新增第三方依赖来源。

### 2.3 流式文本先住在动态区，定稿才落盘

scrollback 里的行一旦打印就不可改写。所以：

- `agent/text_delta` 只更新 `TurnView.text`，渲染在底部动态区（最多 12 行，超出显示尾部）。
- 遇到 `tool/begin` 或 `turn_completed` 时，把这段文本作为 Markdown **一次性提交**到 scrollback。

这与 Ink `<Static>` 的提交时机是同一套语义。

### 2.4 状态行按 Athena 的领域概念组织

Claude Code 的状态行是 model / cwd / token / cost。Athena 是 AI4S 系统，改成：

```
 ● research  thread 3f9c…  turn 2  ⏱ 12.4s  ⚒ 3  ▸ 47 events   [ask]  ^R verbose
```

`session / thread / turn / 耗时 / 工具数 / 事件数 / 审批模式`。
**不显示 token 与花费**——`ResponsesProvider.stream()` 没有开 `stream_options.include_usage`，
拿不到 usage，与其编一个数字不如不显示。

### 2.5 `/fork` 是 Athena 独有的一等公民

Claude Code 没有线程分叉。Athena 的 `thread/fork` 支持从某个已完成 Turn 的上下文快照开新线程，
对"同一份数据、两条研究路线"的场景是刚需，所以做成一等命令。

> 已知后端限制：`RuntimeThreadManager._make_runtime()` 为每个 Thread 新建 `ContextManager`，
> 所以 fork 出的线程继承的是 `context_ref` 快照，**不继承对话消息窗口**。
> TUI 会在 fork 后明确提示这一点，不假装消息被复制了。

## 3. 模块划分

```
src/athena/tui/
  __main__.py      入口：参数解析、.env 加载、装配顺序
  doctor.py        配置自检（--check）：环境变量 → 端点连通 → 流式工具调用
  app.py           AthenaTUI —— 组装 prompt_toolkit Application，事件泵，提交/中断编排
  session.py       TuiSession —— 对 AthenaClient 的领域封装（start/submit/subscribe/fork/interrupt）
  state.py         纯状态层：AppState / TurnView / ToolCallView / Block / reduce_event()
  transcript.py    Block → rich renderable → ANSI 字符串 → scrollback
  live.py          底部动态区的 formatted-text 构造（流式文本、工具行、状态行、提示行）
  composer.py      输入缓冲：多行、历史、排队提交
  completion.py    斜杠命令 + @路径补全
  keys.py          键位绑定
  approvals.py     ServerRequest(item/approval/request) → 内联审批弹窗
  commands.py      斜杠命令注册表与分发
  runner.py        默认 runner 装配：agent profile 注册表 + 审批闸门 + mock runner
  theme.py         颜色与符号（支持 NO_COLOR / ATHENA_TUI_ASCII）
```

**分层规则**：`state.py` 与 `commands.py` 既不 import `prompt_toolkit` 也不 import
`rich`，可以纯函数式地单测。`completion.py` 的补全规则 `candidates()` 同样是纯函数，
只在文件末尾用一个 `AthenaCompleter` 适配到 prompt_toolkit。

## 4. 数据流

```
用户回车
   └─► Composer.take()  ─► 斜杠命令？ ─是─► commands.dispatch() ─► Block[] ─► Transcript
                                     └─否─► session.submit(text)  ("turn/start")
                                                │
   AppServer ──► ThreadRuntime ──► Agent ──► emit(kind, ref, data)
                                                │
                                       EventJournal ─► FairMux ─► EventNotification
                                                │
   事件泵 ─► reduce_event(state, kind, data) ─► Block[] ─► Transcript.commit()
                                            └─► AppState 更新 ─► 底部动态区重绘
```

`reduce_event` 是唯一的状态迁移点，输入 `(kind, data, event_ref)`，输出**已定稿的 Block 列表**，
副作用只有改 `AppState`。所有渲染分支都能脱离终端测试。

### 事件到 Block 的映射

| 事件 kind | AppState 变化 | 提交的 Block |
| --- | --- | --- |
| `turn_started` | 新建 `TurnView` | — |
| `agent/text_delta` | `turn.text = data["accumulated"]` | —（只在动态区） |
| `tool/begin` | 追加 `ToolCallView(running)` | 先 `assistant`（若有未定稿文本），再 `tool_begin` |
| `tool/end` | 标记 ok | `tool_end` |
| `tool/error` | 标记 error | `tool_end` |
| `turn_completed` | 归档到 history | `assistant` + `turn_end` |
| `turn_failed` | 归档 | `assistant` + `turn_end(failed)` |
| `turn_interrupted` | 归档 | `assistant` + `turn_end(interrupted)` |
| 其他 | — | 仅 `--verbose` 时提交 `raw` |

## 5. 交互契约

### 键位

| 键 | 行为 |
| --- | --- |
| `Enter` | 提交；正在运行时进入排队 |
| `Alt+Enter` / `Ctrl+J` | 换行 |
| `Esc` | 中断当前 Turn（`turn/interrupt`） |
| `Ctrl+C` | 有输入→清空；运行中→中断；空闲连按两次→退出 |
| `Ctrl+D` | 输入为空时退出 |
| `Ctrl+L` | 清屏 |
| `Ctrl+R` | 切换 verbose（显示原始事件流） |
| `Shift+Tab` | 循环审批模式 ask → auto → deny |
| `Tab` | 补全 |
| `↑` / `↓` | 首行/末行时翻历史 |
| `y` / `n` / `a` | 审批弹窗：批准 / 拒绝 / 本会话始终批准 |

### 斜杠命令

`/help` `/clear` `/exit` `/quit` `/status` `/threads` `/new` `/switch <id>`
`/fork [turn_id]` `/interrupt` `/verbose` `/mode [ask|auto|deny]` `/events [n]`
`/tools` `/model [name]` `/agent [profile]`

命令全部在 `commands.py` 注册，返回 `Block[]` 或异步动作，与渲染解耦。

### 命令行开关

| 开关 | 作用 |
| --- | --- |
| `--check` | 只做配置自检，不启动界面。见 §6 |
| `--mock` | 用离线 `MockRunner`，不调任何模型 |
| `--model` / `--agent` | 覆盖模型与 agent profile |
| `--mode {ask,auto,deny}` | 启动时的审批模式 |
| `--no-approval` | 关掉审批闸门，工具直接执行 |
| `--verbose` | 启动即显示原始事件流 |
| `--debug` | 日志写 `~/.athena/tui.log` |

`--debug` 必须落文件：日志走 stderr 会直接冲掉底部动态区。不开 `--debug` 时
日志被整体关到 `WARNING` 以下，避免第三方库往终端吐东西。

## 6. 模型配置与自检

模型走 OpenAI 兼容协议，三个环境变量（`.env` 由 `__main__.load_env()` 加载，
先读 CWD 再用项目根兜底，所以从任意目录启动都拿得到）：

| 变量 | 作用 |
| --- | --- |
| `OPENAI_API_KEY` | 密钥 |
| `OPENAI_BASE_URL` | 兼容端点地址；留空走 OpenAI 官方 |
| `ATHENA_TUI_MODEL` | 默认模型，优先级高于 profile 工厂的签名默认值 |

`ATHENA_TUI_MODEL` 读取点放在 `AgentRuntime._probe_default_model()` 而不是入口参数：
`/agent` 切 profile 时也走这里，模型才不会被悄悄换回内置默认值。会话内 `/model` 仍可临时覆盖。

`--check` 逐项验证：环境变量 → agent 能否装配 → `chat.completions` 能否连通 →
**端点是否支持流式 `tool_calls`**。最后一项是关键：Agent loop 完全依赖它，
只测"连得上"没有意义。自检复用 `ResponsesProvider` 而不是自建 client，
测的就是 Agent 实际会走的那条路；密钥打码输出，方便直接贴出来排查。

## 7. 审批通路

`MessageProcessor.request_approval()` 早就存在，但此前无人调用。TUI 侧补齐了另一半：

1. `runner.py` 的 `GatedTool` 包装每个注册工具，调用前 `await gate(ctx, args)`。
2. gate 调 `server.request_approval(thread_id, turn_id, message)` → 发出 `ServerRequest`。
3. TUI 的事件泵收到 `item/approval/request` → 按当前模式决定：
   - `auto` 或工具已在"始终批准"集合 → 立即回 `{"approved": true}`
   - `deny` → 立即回 `{"approved": false}`
   - `ask` → 弹出内联审批框，等用户按 `y` / `n` / `a`
4. `client.respond_to_server_request()` 回复，`GatedTool` 据此放行或返回拒绝的 `ToolResult`。

策略留在 UI 侧，与 Claude Code 的 permission mode 一致：服务端只负责问，不负责判。

## 8. 对后端做的六处最小改动

TUI 本身不改后端语义，但有几处数据缺口不补就渲染不出东西，均为**加性**修改，
不改任何既有 kind、方法名或 `event_ref` 格式（既有测试无一改动、全部通过）：

1. `core/tool.py`：`tool/begin` / `tool/end` 此前 `data=None`，补上
   `{"tool", "call_id", "args"}` 与 `{"tool", "call_id", "ok", "error", "preview",
   "truncated", "duration_ms"}`。完整结果仍只走返回值，事件里只放 600 字预览。
2. `core/agent/agent.py`：`ToolContext` 的 `call_id` 由 `f"{turn_id}:{tool_name}"`
   改为 `f"{turn_id}:{llm_call_id}"`。**这是一个真实缺陷**：原来的写法在同一轮
   并行调用同一个工具时会产生相同的 call_id，UI 无法把 begin 和 end 配对。
3. `core/tool_types.py`：新增 `TOOL_DENIED` 与 `TOOL_PREVIEW_CHARS` 常量。
4. `app_server/server.py`：`request_approval()` 增加可选 `payload` 参数，把工具名和
   参数以结构化字段发给 Client。此前只有一个 `message` 字符串，UI 只能靠解析它取工具名。
5. `core/agent/provider.py`：显式读取 `OPENAI_BASE_URL`（此前只靠 OpenAI SDK 的隐式回退），
   并在取到空串时兜底到官方地址。**空串是个真问题**：SDK 只在环境变量*不存在*时才用官方
   地址，`.env` 里写一行空的 `OPENAI_BASE_URL=` 会让 client 的 base_url 变成空串。
6. `app_server/thread_runtime.py`：`turn_failed` 事件带上 `{"exception_type": ...}`。
   之前失败原因在 `RunnerFailed` 里就被丢掉了，UI 只能显示一个没有信息量的"失败"。
   **只带类型不带消息**——异常消息可能含 prompt 或路径，不该进事件流；完整原因
   用 `--debug` 从 `~/.athena/tui.log` 取。

## 9. 不做的事

- **不做 alt-screen 全屏布局**：会毁掉终端 scrollback，这是 Claude Code 刻意避开的。
- **不做 Vim 模式 / 语音 / IDE bridge**：Claude Code 有，但对当前 Athena 是纯负担。
- **不做会话持久化与 `/resume`**：`RolloutRecorder` 写的是 JSONL 消息流，
  不是 UI 事件流，直接拿来重放转录区会失真。要做得先定 UI 侧的 replay 契约。
- **不显示 token / cost**：见 2.4。

## 10. 测试

`test/unit/tui/` 共 113 个用例，分三层（全仓 547 passed）：

| 层 | 文件 | 手段 |
| --- | --- | --- |
| 纯逻辑 | `test_state.py` `test_commands.py` `test_completion.py` `test_runner.py` `test_doctor.py` | 直接调函数，无 IO |
| 协议 | `test_session.py` `test_approvals.py` | 真实 `AppServer` + `MockRunner` 跑完整事件流 |
| 界面 | `test_app.py` | `create_pipe_input()` + `DummyOutput()` 真的把 Application 跑起来，按键从管道喂进去 |

`test_app.py` 覆盖的是只有跑起来才验证得到的路径：回车提交、运行中排队并在
Turn 结束后自动出队、`Esc` 中断、`Shift+Tab` 循环模式、`Ctrl+C` 两次退出、
以及一次**真实的审批往返**（`server.request_approval()` → transport → 事件泵 →
弹窗 → 按 `y` → `respond_to_server_request` → future 落地）。

写这层时踩到一个真问题：测试发 `"\n"` 全部超时——prompt_toolkit 里 `\n` 是
`Ctrl+J`（绑定为换行），Enter 是 `"\r"`。所以 `run_tui()` 的驱动协程在断言失败时会
立刻 `app.exit()`，否则真正的失败原因会被 20 秒超时盖掉。

### 真机验证

除自动化测试外，用真实端点（阿里云百炼 `qwen3.7-plus`）跑过完整 Turn：
用户输入 → 流式文本 → `list_dir` 工具调用 → 结果预览 → Markdown 定稿 → 统计尾行，
状态 `completed`，47 个事件。

# Pull Request：TaskUnderstandingAgent + Agent 认知子系统 + MCP 接入层 + Sandbox Executor

日期：2026-08-08
分支：`TaskUnderstandingAgent` → `main`

---

## 概述

本 PR 包含四个紧密关联的功能模块：

1. **通用 MCP 接入层**：配置驱动的外部工具接入框架，Kaggle + Sandbox 为接入方。采用 Codex 式"搜索激活"模型，零代码接入新 MCP server。
2. **TaskUnderstandingAgent**：竞赛任务理解与执行 Agent，集成 11 个原生工具，通过 ReAct 自主循环完成从"理解需求"到"产出交付"的完整 pipeline。
3. **Agent 认知子系统**：通用认知管理框架（`CognitionManager` + 5 个认知工具），为长工作流提供结构化规划、进度追踪、阶段反思和约束守卫能力。
4. **Sandbox Executor**：进程级隔离的 Python 代码执行沙箱（以独立 MCP Server 暴露），Agent 可自主执行数据分析代码并观察结果。

---

## 一、通用 MCP 接入层

### 1.1 设计动机

原有 3 个 Kaggle 工具（`kaggle_competition_search` / `kaggle_discussion_search` / `kaggle_dataset_download`）是 stub，从未真正连通 Kaggle。此次构建了一个通用 MCP 接入层，让 Kaggle 官方 MCP server 和 Sandbox Server 成为接入方，并支持未来零代码接入任意 MCP server。

### 1.2 模块结构

```
src/athena/tools/mcp/
  __init__.py       # register_mcp_tools() 装配入口
  config.py         # McpServerConfig dataclass + JSON 加载器
  client.py         # McpClientManager：连接/ list_tools / call_tool / 重连 / manifest 缓存
  adapter.py        # McpToolAdapter(BaseTool)：由 MCP 工具定义自动构建
  search.py         # McpSearchTools(BaseTool)：全局搜索 + 激活注册
```

### 1.3 Codex 式"搜索激活"模型

**常驻工具（每轮请求全量 schema）**：

1. Athena 原生工具（HF 数据集/模型、认知工具等）
2. 全局入口 `mcp_search_tools`——"搜索所有已接入的外部 MCP 服务可用工具"

**钉住工具（构建期注册，始终可用）**：Sandbox 的 `python_inspect`、`python_execute`、`sandbox_config`，因为数据分析是核心能力，不应依赖 LLM 搜索发现。

**默认隐藏**：Kaggle MCP 工具的 schema 不在首轮清单。LLM 需要时先调 `mcp_search_tools`，命中 → 注册进 ToolRegistry → **下一轮 LLM 请求即可见可调**。

```
第 1 轮: LLM 只看到 [原生工具…, mcp__sandbox__python_inspect, …, mcp_search_tools]
  → LLM 调用 mcp_search_tools {query: "提交竞赛结果"}
  → 返回 "kaggle__submit_competition" + 精简 schema，并注册进 ToolRegistry
第 2 轮: LLM 请求中出现 kaggle__submit_competition → 直接调用
```

### 1.4 配置驱动

`mcp_servers.json`：

```json
{
  "servers": [
    {
      "name": "sandbox",
      "transport": "stdio",
      "command": "${PYTHON_EXECUTABLE}",
      "args": ["-m", "athena.sandbox", "--work-root", "${WORK_ROOT}"],
      "tool_prefix": "mcp__sandbox__",
      "pinned_tools": ["python_inspect", "python_execute", "sandbox_config"]
    },
    {
      "name": "kaggle",
      "transport": "streamable_http",
      "url": "https://www.kaggle.com/mcp",
      "auth": { "type": "bearer", "env": "KAGGLE_API_TOKEN" },
      "tool_prefix": "kaggle__",
      "pinned_tools": []
    }
  ]
}
```

- `${WORK_ROOT}` 和 `${PYTHON_EXECUTABLE}` 占位符在 `register_mcp_tools()` 中由 `dataclasses.replace()` 展开
- `transport`：`streamable_http`（远程）或 `stdio`（本地子进程）
- `auth`：v1 支持 bearer token，值从 `.env` 读取
- `tool_prefix`：防多 server 命名冲突，发现的工具名 = `{prefix}{mcp_tool_name}`
- `pinned_tools`：Sandbox 3 个核心工具钉住注册，Kaggle 工具按需搜索激活
- **未配置任何 server → 不注册搜索工具，接入层完全惰性，现有行为不变**

### 1.5 连接生命周期

- Sandbox `pinned_tools` 非空 → 构建期建立 MCP 连接，拉取工具 schema
- Kaggle `pinned_tools=[]` → 首次调用 `mcp_search_tools` 或首个被发现的工具时才建立连接
- 搜索在缓存 manifest 上匹配，无网络开销
- 调用失败先重连一次 + 指数退避重试；单次调用超时默认 120s
- Agent 运行结束（`Agent.run()` finally 块）主动关闭所有 MCP 连接

### 1.6 工具适配与安全

**Schema 映射**：`tools/list` 返回的 `name` / `description` / `inputSchema` 自动映射到 `ToolSpec`。

**结果归一化**：
- 文本 parts 拼接
- `structuredContent` 优先转 JSON
- 图片/资源块落盘保存
- 返回给 LLM 精简摘要（不把整个载荷塞上下文）

**落盘规则**：`{work_root}/mcp/{server}/{tool}/result.json`

**文件下载安全**：
- 只跟随工具自身返回的 URL，绝不跟随 LLM 提供的 URL
- 下载文件只写入该工具管理的输出目录
- token 只在 header 传递、不打日志

**Sandbox 终端输出**：`McpToolAdapter` 对 `python_inspect` / `python_execute` 工具的调用参数（表达式/脚本）和执行结果（stdout/stderr/value/error）打印到终端 stdout，方便开发者观察 Agent 的数据分析过程。

### 1.7 Kaggle 接入

原有的 3 个 Kaggle stub 已删除，全部 Kaggle 能力改由 MCP 自动发现提供。配置 `mcp_servers.json` 中指向 `https://www.kaggle.com/mcp`，Agent 运行时通过 `mcp_search_tools` 搜索并激活所需工具。

---

## 二、Agent 认知子系统

### 2.1 核心问题

LLM 上下文窗口有限，长工作流面临四个痛点：

| 痛点 | 表现 | 解决方案 |
|------|------|----------|
| 上下文遗忘 | LLM 记不清前面的发现和决策，重复劳动 | Plan 树 + Checkpoint 持久化到 StateStore |
| 缺乏规划 | 拿到任务就直接干，走弯路 | `agent_plan` 强制先规划再执行 |
| 失败恢复难 | 某步骤失败后从头再来 | Checkpoint 记录阶段产物，可从断点恢复 |
| 子任务混乱 | 分不清依赖关系，执行顺序乱 | Plan 树的 `relation` 字段定义 sequential/parallel/alternative |

### 2.2 模块结构

```
src/athena/cognition/
  __init__.py              # register_cognition_tools() 装配入口
  manager.py               # CognitionManager —— 核心状态机
  schemas.py               # PlanNode, Plan, Checkpoint, Reflection, GuardRule, TrackState
  tools/
    plan.py                # AgentPlanTool —— 规划（强制）
    checkpoint.py          # AgentCheckpointTool —— 检查点（强制）
    reflect.py             # AgentReflectTool —— 反思（可选）
    guard.py               # AgentGuardTool —— 约束守卫（可选）
    track.py               # AgentTrackTool —— 进度追踪（可选）
```

### 2.3 五个认知工具

| 工具 | 类型 | 用途 |
|------|------|------|
| `agent_plan` | 强制 | 创建/更新树形执行计划，任务开始前必须调用 |
| `agent_checkpoint` | 强制 | 在节点标记里程碑，记录阶段产出 |
| `agent_track` | 可选 | 查询当前进度：位置、下一步、全局摘要 |
| `agent_reflect` | 可选 | 阶段性回顾，检查是否偏离目标，生成调整建议 |
| `agent_guard` | 可选 | 检查即将执行的操作是否违反硬约束 |

### 2.4 Plan 树结构

Plan 不是线性步骤列表，而是一棵树。`relation` 字段决定子节点执行关系：

| relation | 含义 | 示例 |
|----------|------|------|
| `sequential` | 顺序执行 | Step1 → Step2 → Step3 |
| `parallel` | 可并发 | 同时下载多个文件 |
| `alternative` | 备选方案 | ls+cat vs pandas，择优选用 |

每个节点有五种状态：`pending` → `in_progress` → `completed` / `failed` / `skipped`。

### 2.5 Mermaid 可视化

每次状态变更自动生成 `{work_root}/cognition/plan.mermaid`，状态到样式映射：

| status | 图标 | 颜色 |
|--------|------|------|
| `pending` | ⬜ | 灰 |
| `in_progress` | 🔄 | 蓝 |
| `completed` | ✅ | 绿 |
| `failed` | ❌ | 红 |
| `skipped` | ⏭️ | 深灰 |

### 2.6 智能提示机制

不在 system prompt 中写长篇使用规范。CognitionManager 在运行时按需注入提示：

- **工具类别迁移检测**：滑动窗口（最近 3 次工具调用）检测阶段切换，自动建议 `agent_checkpoint`
- **agent_track 主动查询检测**：Agent 查询下一步时，提醒标记完成当前步骤
- **Plan 首次调用提示**：首次 turn 未调用 `agent_plan` 时注入简短建议

不阻断执行，完全由 Agent 自主决定。

### 2.7 工具类别标签

给每个工具打类别标签（存于 CognitionManager 内部，不暴露给 LLM）：

| 类别 | 示例工具 |
|------|---------|
| `search` | mcp_search_tools、hf_dataset_search、MCP 搜索类工具 |
| `download` | hf_dataset_download、MCP 下载类工具 |
| `inspect` | python_inspect、探查型 python_execute |
| `compute` | 清洗/训练脚本 python_execute、数据转换 |
| `artifact` | agent_checkpoint、文件写入 |

### 2.8 持久化布局

```
{work_root}/cognition/
  plan.json              # Plan 树完整 JSON
  plan.mermaid           # Mermaid 可视化文件
  checkpoints/           # 阶段性检查点
  reflections/           # 反思记录
  guard_rules.json       # 硬约束规则（开发者手动配置）
```

---

## 三、TaskUnderstandingAgent

### 3.1 工具链总览

| 层级 | 工具 | 状态 |
|------|------|------|
| HF 数据集 | `hf_dataset_search` | ✅ 已测通 |
| | `hf_dataset_download` | ✅ 已测通 |
| HF 模型 | `hf_model_search` | ✅ 已测通 |
| | `hf_model_download` | ✅ 已测通 |
| 认知子系统 | `agent_plan` | ✅ |
| | `agent_checkpoint` | ✅ |
| | `agent_track` | ✅ |
| | `agent_reflect` | ✅ |
| | `agent_guard` | ✅ |
| Sandbox MCP | `mcp__sandbox__python_inspect` | ✅ 钉住注册 |
| | `mcp__sandbox__python_execute` | ✅ 钉住注册 |
| | `mcp__sandbox__sandbox_config` | ✅ 钉住注册 |
| 搜索入口 | `mcp_search_tools` + 动态发现的 Kaggle 工具（`kaggle__*`） | ✅ |

- 原生工具共 11 个（4 HF + 5 cognition + 1 mcp_search_tools + 3 sandbox MCP 钉住）
- Kaggle MCP 工具按需动态激活
- 所有工具执行产物直接落盘到 `{work_dir}/{tool_name}/` 子目录
- 工具按字母序排列注册，保证 prompt cache 稳定

### 3.2 Demo 脚本

`demo_taskunderstand_agent.py` 提供两种运行模式：

- **seeded 模式**：预注入 Titanic 竞赛元数据，绕过外部依赖，完成端到端 pipeline 验证
- **auto 模式**：完全自主 ReAct 循环（通过 MCP 接入层搜索和使用 Sandbox/Kaggle 工具）

```bash
# 默认 auto 模式
python demo_taskunderstand_agent.py

# 使用 HF 镜像
python demo_taskunderstand_agent.py --hf-endpoint https://hf-mirror.com

# 自定义参数
python demo_taskunderstand_agent.py --max-turns 30 --competition "titanic"
```

Demo 输出包含：
- 实时 LLM 文本流
- 工具调用开始/成功/失败标记
- **Sandbox 执行代码和结果**（通过 `McpToolAdapter` 的 stdout 输出）
- **GuardError 诊断信息**（`guard_interrupted` + `guard_reason`）
- 工具调用统计和产物目录清单

### 3.3 Agent 构建入口

```python
from athena.agents.competition.task_understand_agent import build_task_understand_agent

agent = await build_task_understand_agent(
    model="deepseek-v4-flash",
    client=client,
    work_root="work",
    max_turns=30,
)
# 可选：注入 MCP server 配置
# agent = await build_task_understand_agent(..., mcp_servers=[McpServerConfig(...)])
```

---

## 四、Sandbox Executor

### 4.1 设计动机

旧数据准备层（`DataAnalyzeTool` + `DataCleanCodeGenTool`）太死板：把文件样本丢给 LLM 做单轮 JSON 输出，Agent 没有真正"操作数据、观察结果、迭代深入"的能力。

Sandbox Executor 让 Agent 能**自主执行 Python 数据分析代码并观察运行结果**，以独立 MCP stdio server 暴露，完全解耦、跨 Agent 复用。

### 4.2 模块结构

```
src/athena/sandbox/
├── __init__.py
├── __main__.py           # python -m athena.sandbox 入口（MCP stdio server）
├── server.py             # MCP server 实现（3 个工具定义 + JSON-RPC stdio）
├── executor.py           # SandboxExecutor —— wrapper script 生成 + subprocess 隔离执行
├── limits.py             # SandboxLimits —— 跨平台资源限制（内存 2048MB）
├── lifecycle.py          # ProcessLifecycle —— 孤儿进程清理
└── whitelist.py          # import 白名单 + SafeOS 代理
```

### 4.3 隔离与安全（多层限制）

| 层 | 机制 | 用途 |
|---|---|---|
| **Import 白名单** | `builtins.__import__` monkey-patch | 阻止任意模块加载 |
| **Safe OS 代理** | `sys.modules['os']` 替换 | 允许文件操作（listdir/makedirs/stat），禁止 `os.system`/`popen`/`environ`/`chmod` 等 |
| **进程超时** | threading.Thread + asyncio 轮询 | inspect 10s/30s, execute 60s/120s |
| **内存限制** | 平台感知（Linux: RLIMIT_AS, Windows: Job Object） | 2048MB |
| **路径隔离** | wrapper 中 `os.chdir(cwd)`，拒绝 `..` 穿越 | 子进程限制在 work_root 内 |

### 4.4 三个 MCP 工具

| 工具 | 用途 | 示例 |
|------|------|------|
| `python_inspect` | 执行单行表达式，返回 repr + type/shape/columns | `df.head()`, `len(df)`, `os.listdir('.')` |
| `python_execute` | 执行多行脚本，返回 stdout/stderr/output_files | 清洗脚本、特征工程、画图保存 |
| `sandbox_config` | 返回 work_root、白名单、超时限制 | Agent 自我校准"我有哪些库和限制" |

### 4.5 Wrapper Script 模板

每个 inspect/execute 调用生成一个 wrapper script，在独立子进程中执行：

```
1. 资源限制（SandboxLimits.apply_memory_limit()）
2. Import 白名单 monkey-patch（builtins.__import__）
3. Safe OS 代理（sys.modules['os'] = build_safe_os()）
4. 预置库 import（pd/np/plt/sns/sklearn）
5. 工作目录切换（os.chdir(cwd)）
6. 执行用户代码（eval(expr) 或 exec(script)）
7. 输出标记行 ___SANDBOX_RESULT_START___/___END___ + JSON
```

### 4.6 Windows 兼容性

在 Windows Python 3.14 + FastMCP 环境下，`asyncio.create_subprocess_exec` 在 MCP stdio transport 的事件循环中会永久阻塞 pipe I/O。解决方式：`_run_subprocess` 使用 `subprocess.Popen` + `threading.Thread` + `asyncio.sleep(0.1)` 轮询，完全绕开 asyncio 事件循环。

---

## 五、RunGuard 硬约束

### 5.1 设计动机

不在 System Prompt 中写防死循环规则（不可靠——LLM 会忘记），而是在运行时层面做硬约束。

### 5.2 三条规则

| 规则 | 触发条件 | 结果 |
|---|---|---|
| **完全相同的调用** | 同一 `(tool, args_hash)` 再次出现 | 调用前拒绝，返回 error 给 Agent |
| **同一工具连续失败** | 同一 tool 连续 4 次 `success=False` | 抛 `GuardError`，Turn 终止 |
| **同一错误模式循环** | 同一 `(tool, err_type)` 累计 4 次 | 抛 `GuardError`，Turn 终止 |

中间有成功调用 → 连续失败计数器清零。

### 5.3 注入位置

1. `Agent.run()` 开始时创建 `RunGuard` 实例，挂到 `AgentContext.guard`
2. `_sampling_loop()` 中每个工具调用前 → `guard.check_before_call()`
3. `_sampling_loop()` 中每个工具返回后 → `guard.record_result()`

### 5.4 GuardError 行为

`GuardError` 被抛出 → `Agent.run()` 捕获 → 返回带 `guard_interrupted=True` 的 `AgentOutcome` → **Turn 终止，Thread 存活**。Demo 脚本和 `run_summary.json` 均展示 `guard_interrupted` 和 `guard_reason` 字段，便于诊断。

---

## 六、架构集成

```
TaskUnderstandAgent (build_task_understand_agent)
  │
  ├── 原生工具 (11 个)
  │   ├── HF 数据集 (2): hf_dataset_search, hf_dataset_download
  │   ├── HF 模型 (2): hf_model_search, hf_model_download
  │   └── 认知子系统 (5): agent_plan, agent_checkpoint, agent_track,
  │                         agent_reflect, agent_guard
  │
  ├── Sandbox MCP（钉住注册，始终可用）
  │   ├── mcp__sandbox__python_inspect
  │   ├── mcp__sandbox__python_execute
  │   └── mcp__sandbox__sandbox_config
  │
  └── MCP 接入层 (动态)
      ├── mcp_search_tools ──→ 搜索激活 ──→ kaggle__* 等
      ├── McpClientManager ──→ 连接/调用/缓存/清理
      └── McpToolAdapter ────→ schema 映射/结果归一化/落盘/终端输出

Agent ReAct 循环
  ├── RunGuard ──→ 调用前准入 + 调用后记录（硬约束防死循环）
  ├── tool call → Sandbox MCP ──→ subprocess 隔离执行
  └── tool call → CognitionManager → StateStore 持久化
```

---

## 七、Debug 与问题修复

在集成和端到端测试过程中发现并修复了以下问题：

### 7.1 Sandbox MCP 工具未注册

**问题**：`KeyError: "Tool 'mcp__sandbox__python_inspect' not found"`  
**原因**：`mcp_servers.json` 中 `pinned_tools: []` 为空，Sandbox 工具未被注册  
**修复**：将 3 个 Sandbox 工具加入 `pinned_tools`

### 7.2 `${WORK_ROOT}` 占位符未展开

**问题**：MCP server 收到的 work_root 是字面量 `${WORK_ROOT}`，而非实际路径  
**修复**：在 `register_mcp_tools()` 中添加 `_expand_vars()` / `_expand_args()` 函数，用 `dataclasses.replace()` 展开 `${WORK_ROOT}` 和 `${PYTHON_EXECUTABLE}`

### 7.3 Windows `uv` 命令找不到

**问题**：`[WinError 2] 系统找不到指定的文件` — MCP 子进程找不到 `uv`  
**原因**：MCP SDK 的 `create_windows_process` 限制了子进程环境变量，`PATH` 被精简  
**修复**：将 command 改为 `${PYTHON_EXECUTABLE}`（当前 Python 解释器），args 改为 `["-m", "athena.sandbox", ...]`

### 7.4 python_inspect/python_execute 超时

**问题**：所有 sandbox 工具调用在 30s 后超时  
**原因**：FastMCP + anyio 在 Windows Python 3.14 下，`asyncio.create_subprocess_exec` 和 `asyncio.to_thread()` 的 pipe I/O 在 MCP stdio transport 事件循环中永久阻塞  
**修复**：`_run_subprocess` 改用 `subprocess.Popen` + `threading.Thread` + `asyncio.sleep(0.1)` 轮询，完全绕开事件循环

### 7.5 anyio teardown 异常

**问题**：Agent 关闭时出现 `RuntimeError: Attempted to exit cancel scope in a different task` 等错误  
**原因**：Python 3.14 + anyio 兼容性问题，MCP 连接在垃圾回收时触发异步生成器清理异常  
**修复**：`Agent.run()` 添加 `finally` 块主动关闭所有 `McpClientManager`；`McpClientManager.close()` 吞掉已知的 anyio teardown 异常

### 7.6 沙箱执行代码和结果不可见

**问题**：开发者无法看到 Sandbox 中执行的 Python 代码和运行结果  
**初步方案**：在 `executor.py` 中用 `_logger.info()` 输出到 stderr → 被 MCP 协议日志淹没，且在部分终端中 stderr 不可见  
**最终方案**：在 `McpToolAdapter.execute()` 中对 `python_inspect`/`python_execute` 工具通过 `print()` 输出到 stdout（`[SANDBOX INSPECT]`/`[SANDBOX EXECUTE]`/`[SANDBOX STDOUT]`/`[SANDBOX STDERR]`/`[SANDBOX ERROR]`/`[SANDBOX VALUE]`），与 demo 输出同流；`__main__.py` 强制 `sys.stderr` UTF-8 编码并抑制 MCP 协议日志噪音

### 7.7 GuardError 诊断信息缺失

**问题**：Demo 运行异常结束时无法判断是否触发了 GuardError 及具体原因  
**修复**：Demo 终端输出和 `run_summary.json` 中添加 `guard_interrupted` 和 `guard_reason` 字段

---

## 八、测试

| 层级 | 位置 | 内容 |
|------|------|------|
| MCP 接入层单测 | `test/unit/tools/mcp/` | 内存假 server 验证：schema 映射、搜索匹配 + 激活注册、call_tool 归一化、落盘、下载、错误包装 |
| 工具单测 | `test/unit/tools/` | 全部原生工具的输入输出验证 |
| 认知子系统单测 | `test/unit/cognition/` | Plan 树 CRUD、状态转换、Mermaid 渲染、Guard 匹配 |
| RunGuard 单测 | `test/unit/agent/` | 15 个测试覆盖全部规则和并发安全 |
| Sandbox 单测 | `test/unit/sandbox/` | 进程生命周期绑定、资源限制、import 白名单 + SafeOS 代理 |
| Sandbox Executor 单测 | `test/unit/sandbox/` | inspect/execute 基础功能、DataFrame 类型推断、import 白名单拦截、超时 |
| MCP 集成测试 | `test/integration/` | Sandbox MCP 连接/工具发现/python_inspect/sandbox_config；Kaggle MCP 由 `KAGGLE_API_TOKEN` 门控 |
| 端到端 | `demo_taskunderstand_agent.py` | Titanic 完整 pipeline（真实 LLM API + HuggingFace Hub + Sandbox MCP） |

---

## 九、未完成（后续 PR）

- **data_prepare 流程优化**：当前 EDA 基于扫描目录 + 样本传 LLM 可用，理想方案需自主抽样策略 + 多轮分析 Loop
- **建模/提交工具**：`solution_design`、`project_code_gen`、`code_execute`、`submission_build` 已删除，待基于 Sandbox 的新方案
- **认知子系统高级特性**：跨 Session Plan 恢复、Guard 规则动态学习、分布式多 Agent 协同
- **MCP 接入层增强**：OAuth 2.0 流程（v1 仅 bearer token）、基于使用频率的自动钉住、`resources` / `prompts` 暴露
- **Sandbox 增强**：文件操作工具化（不依赖白名单 os）、进程间状态共享、增量执行上下文

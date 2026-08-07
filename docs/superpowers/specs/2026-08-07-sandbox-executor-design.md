# Athena Sandbox Executor 设计

Status: approved
Created: 2026-08-07
Owner: TaskUnderstandingAgent 分支

## 背景

当前数据准备层（`DataAnalyzeTool` + `DataCleanCodeGenTool`）太死板：把文件样本丢给 LLM
做单轮 JSON 输出，Agent 没有真正"操作数据、观察结果、迭代深入"的能力。TaskUnderstandingAgent
中已注释掉这两个工具。

## 目标

Agent 拿到数据集后，自主设计方案探索数据结构、执行清洗/预处理、产出 `eda.md`。
核心需求：Agent 能**执行 Python 数据分析代码并观察运行结果**。

## 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 执行代码类型 | 仅 Python 数据分析脚本（pandas/numpy/matplotlib/sklearn 等） | 聚焦数据科学场景 |
| 隔离方式 | 进程级隔离：subprocess + import 白名单 + 超时/内存限制 | 足够安全，实现简单 |
| 执行模型 | 混合模式：默认 stateless，Agent 自行决定何时将中间结果落盘 | 平衡简单性与灵活性 |
| 暴露方式 | 独立 MCP Server（stdio transport），任何 Agent 通过 `register_mcp_tools()` 发现 | 完全解耦，跨 Agent 复用，符合现有 McpClientManager 基础设施 |
| 工具数量 | 3 个：`python_inspect`、`python_execute`、`sandbox_config` | 文件操作通过白名单 `os`/`pathlib` 完成，无需额外 MCP 工具 |
| 平台支持 | Windows / Linux / macOS，平台感知的资源限制与生命周期管理 | 开发环境是 Windows 11，生产环境可能 Linux |

## 架构

```
mcp_servers.json
  {
    "name": "sandbox",
    "transport": "stdio",
    "command": "uv",
    "args": ["run", "python", "-m", "athena.sandbox.server"]
  }
        ↓ McpClientManager (stdio_client)
┌─────────────────────────────────────────┐
│  athena.sandbox.server (MCP stdio)       │
│                                          │
│  Tools:                                  │
│  ┌──────────────────┐ ┌────────────────┐ │
│  │ python_inspect    │ │ python_execute │ │
│  │ · expr: str       │ │ · script: str  │ │
│  │ → repr(value)     │ │ · cwd: str     │ │
│  │   + type/shape    │ │ → stdout+stderr│ │
│  └───────┬──────────┘ │ + output_files  │ │
│          │            └───────┬────────┘ │
│          └────────┬───────────┘          │
│          ┌────────┴───────────┐          │
│          │  SandboxExecutor   │          │
│          │  · subprocess 隔离  │          │
│          │  · import 白名单    │          │
│          │  · 超时 + 内存限制  │          │
│          └────────────────────┘          │
└─────────────────────────────────────────┘
        ↓ Tool calls (JSON-RPC via stdio)
┌─────────────────────────────────────────┐
│  Agent (ReAct loop)                     │
│  ┌─────────────────────────────────────┐│
│  │ ToolRegistry                        ││
│  │  ├─ mcp__sandbox__python_inspect    ││
│  │  ├─ mcp__sandbox__python_execute    ││
│  │  └─ mcp__sandbox__sandbox_config    ││
│  └─────────────────────────────────────┘│
└─────────────────────────────────────────┘
```

## 目录结构

```
src/athena/sandbox/
├── __init__.py
├── __main__.py           # python -m athena.sandbox 入口
├── server.py             # MCP server 实现（工具定义 + JSON-RPC stdio）
├── executor.py           # SandboxExecutor —— 生成 wrapper_script + subprocess.run
├── limits.py             # SandboxLimits —— 平台感知资源限制
├── lifecycle.py          # ProcessLifecycle —— 孤儿进程清理
└── whitelist.py          # import 白名单 + _SafeOS 代理
```

## 模块职责

### `whitelist.py`

- `ALLOWED_IMPORTS: frozenset[str]` — 允许的 import 白名单
- `build_safe_os()` — 返回替换 `sys.modules['os']` 的代理对象，只暴露 `listdir`、`makedirs`、`stat`、`remove`、`rename`、`rmdir`、`getcwd`、`sep`、`path.*`，禁用 `system`、`popen`、`exec*`、`kill` 等。`__getattr__` fallback 为 `PermissionError`
- 同时允许 `pathlib.Path`（底层 `os` 调用被上述代理透明拦截）

**白名单（第一版）**：
`pandas`, `numpy`, `matplotlib`, `seaborn`, `scipy`, `sklearn`,
`collections`, `itertools`, `math`, `statistics`, `json`, `csv`,
`pathlib`, `io`, `typing`, `datetime`, `warnings`

不包含 `os` 直接访问（走 `_SafeOS` 代理）、`sys`、`subprocess`、`shutil`、`importlib`、`socket`、`requests`。

### `limits.py`

`SandboxLimits` 类，平台感知的子进程资源限制。由 wrapper 子进程调用
`apply_memory_limit()` 限制自身内存：

| 机制 | Linux | macOS | Windows |
|---|---|---|---|
| **内存限制 (512MB)** | `resource.RLIMIT_AS` | `resource.RLIMIT_AS` | Win32 Job Object `JOB_OBJECT_LIMIT_PROCESS_MEMORY` |
| **超时** | `asyncio.wait_for(timeout=N)` | 同 | 同 |

启动时 `_ensure_platform_support()` 检测平台能力，不支持时抛 `RuntimeError`。

wrapper 子进程的清理不由 `limits.py` 负责——sandbox server 通过 `_running_subprocesses` 集合
追踪所有运行中的子进程，server 退出时统一 kill。

### `lifecycle.py`

`ProcessLifecycle` 类：
- `bind_to_parent()` — sandbox server **自身**启动时调用，确保 Agent 进程退出时 sandbox server 被 OS 清理
- Linux/macOS：`prctl(PR_SET_PDEATHSIG, SIGKILL)`
- Windows：由 Agent 侧通过 Job Object `KILL_ON_JOB_CLOSE` 处理（Agent 启动 sandbox server 子进程后立即将其赋给 Job Object）
- 注意与 `limits.py` 的层级区别：`limits.py.apply_memory_limit()` 在 wrapper 子进程中调用，限制**数据分析子进程**的资源；`lifecycle.py.bind_to_parent()` 在 sandbox server 进程中调用，确保 **sandbox server 自身**不成为孤儿

### `executor.py`

`SandboxExecutor` 核心：

两个入口方法共用一个模板骨架：
- `async inspect(expr, cwd, timeout=10) → InspectResult`
- `async execute(script, cwd, timeout=60) → ExecuteResult`

wrapper_script 模板按顺序注入：
1. 平台资源限制（`SandboxLimits.apply_memory_limit()`）
2. Import 白名单 monkey-patch（`builtins.__import__`）
3. Safe OS 代理（`sys.modules['os'] = build_safe_os()`）
4. 用户 preamble（`pd`/`np` 等预置 import）
5. 工作目录切换（`os.chdir(cwd)`）
6. 执行用户代码（`eval(expr)` 或 `exec(script)`）
7. 输出标记行 `___SANDBOX_RESULT___` + JSON

inspect 模式差异：
- preamble 只含 `pd`/`np`
- `eval(expr, {"__builtins__": __builtins__}, {})` — 只求值，不允许定义函数/类

execute 模式差异：
- preamble 含 `pd`/`np`/`plt`/`sns`/`sklearn`
- `matplotlib.use('Agg')` 禁用 GUI 后端
- `exec(compiled, _user_globals)` 在预置 namespace 中执行
- 如果用户代码赋值 `_result = ...`，自动捕获摘要

子进程启动：`asyncio.create_subprocess_exec(sys.executable, "-c", wrapper, cwd=work_root)`，捕获 stdout/stderr，解析标记行。

### `server.py`

MCP 协议层（使用 `mcp` 库的 `Server` + `stdio_server`）：
- `@server.list_tools()` → 返回 3 个工具定义
- `@server.call_tool()` → 根据 tool name 分发到 `executor.inspect()` / `executor.execute()` / 返回 config
- `__main__.py` 解析 CLI 参数（`--work-root`、`--allowed-imports`），启动 stdio server

## MCP 工具契约

### `python_inspect`

```
输入:
  expr:       str    # 单行 Python 表达式
  cwd:        str?   # 工作目录（相对 work_root），默认 "."
  timeout:    int?   # 秒，默认 10，最大 30

输出:
  ok:         bool
  value:      str?   # repr(expr_result)，截断至 2000 字符
  type:       str?   # 如 "<class 'pandas.core.frame.DataFrame'>"
  shape:      str?   # DataFrame/ndarray → "(rows, cols)"
  columns:    [str]? # DataFrame → 列名列表
  error:      str?   # 异常信息
  traceback:  str?   # 异常 traceback（截断至 800 字符）
  duration_ms: int
```

使用场景：`df.head()`, `len(df)`, `df['col'].unique()`, `df.describe()`, `os.listdir('.')`

### `python_execute`

```
输入:
  script:     str    # Python 脚本（代码块）
  cwd:        str?   # 工作目录，默认 "."
  timeout:    int?   # 秒，默认 60，最大 120

输出:
  ok:         bool
  stdout:     str    # 标准输出，截断至 5000 字符
  stderr:     str?   # 标准错误
  output_files: [str]# 执行后新增/修改的文件列表（相对 work_root）
  error:      str?   # 异常信息
  traceback:  str?   # traceback
  duration_ms: int
  memory_mb:  float? # 峰值内存（MB）
```

使用场景：清洗脚本、特征工程、画图保存

### `sandbox_config`

```
输入: 无

输出:
  work_root:   str    # 工作根目录绝对路径
  whitelist:   [str]  # 允许的 import 列表（排序）
  timeout_max: {inspect: 30, execute: 120}
  memory_limit_mb: 512
  platform:    str    # "win32" | "linux" | "darwin"
```

使用场景：Agent 自我校准"我有哪些库和限制"

## 隔离与安全

### 多层限制

| 层 | 机制 | 用途 |
|---|---|---|
| **Import 白名单** | `builtins.__import__` monkey-patch | 阻止任意模块加载 |
| **Safe OS 代理** | `sys.modules['os']` 替换 | 允许文件操作，禁止 `os.system`/`popen`/`environ`/`chmod` 等 |
| **进程超时** | `asyncio.wait_for(proc.communicate(), timeout)` | inspect 10s/30s, execute 60s/120s |
| **内存限制** | 平台感知（RLIMIT_AS / Job Object） | 512MB |
| **路径隔离** | wrapper 中 `os.chdir(cwd)`，拒绝 `..` | 子进程限制在 work_root 内 |

### 进程生命周期

| 场景 | 机制 | 效果 |
|---|---|---|
| Agent 正常结束 | `McpClientManager.close()` → stdin 管道关闭 | sandbox server 优雅退出 |
| 用户 Ctrl+C Agent | Win32 Job Object `KILL_ON_JOB_CLOSE` 或 `prctl(PR_SET_PDEATHSIG)` | OS 级强制清理整个进程树 |
| sandbox server 崩溃 | MCP client 检测连接断开 → 抛 `McpClientError`（已有重连+指数退避逻辑，最多 3 次） | Agent 的 ReAct loop 收到 `success=False` |
| python_execute 超时 | `asyncio.wait_for` + `proc.kill()` | 子进程被杀，sandbox server 继续运行 |

## RunGuard：Agent Loop 硬约束

不在 System Prompt 中写防死循环规则（不可靠——LLM 会忘记），而是在运行时层面做硬约束。

### 数据结构

```python
@dataclass
class RunGuard:
    """每个 Agent.run() 调用创建一个 fresh 实例。"""

    max_identical_calls: int = 1        # 同一 (tool, args) 最多调用 1 次
    max_consecutive_failures: int = 3   # 同一 tool 连续失败上限
    max_same_approach_failures: int = 3 # 同一 (tool, error_type) 累计上限

    _call_hashes: set[tuple[str, int]]          # (tool_name, hash(args_json))
    _consecutive_failures: dict[str, int]        # tool_name → 连续失败次数
    _error_patterns: dict[tuple[str, str], int]  # (tool_name, err_key) → 累计次数
```

### 三条规则

| 规则 | 触发条件 | 结果 |
|---|---|---|
| **完全相同的调用** | `(tool, args_hash)` 重复 | 调用前拒绝，返回 error 给 Agent |
| **同一工具连续失败** | 同一 tool 连续 3 次 `success=False` | 抛 `GuardError`，Turn 终止 |
| **同一错误模式循环** | 同一 `(tool, err_type)` 累计 3 次 | 抛 `GuardError`，Turn 终止 |

中间有成功调用 → 连续失败计数器清零。

### 注入位置

1. `Agent.run()` 开始时创建 `RunGuard` 实例，挂到 `AgentContext.guard`
2. `_sampling_loop()` 中每个工具调用前 → `guard.check_before_call()`
3. `_sampling_loop()` 中每个工具返回后 → `guard.record_result()`
4. 主循环每次迭代前 → `guard.check_enter_loop()`

### GuardError 行为

`GuardError` 被抛出 → `Agent.run()` 捕获 → 返回带 error 文本的 `AgentOutcome` → `ThreadRuntime` 记录到 Turn 历史 → **Turn 终止，Thread 存活**。用户可以发起下一个 Turn（换个方向），`RunGuard` 随新 Turn 全新初始化。

## Agent 集成

### TaskUnderstandingAgent 工具注册变更

```python
# 旧（删除）
# from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool
# tools.register(DataAnalyzeTool(...))
# tools.register(DataCleanCodeGenTool(...))

# 新（MCP 自动发现）
# mcp_servers.json 中配置 sandbox server
# register_mcp_tools() 自动发现并注册:
#   mcp__sandbox__python_inspect
#   mcp__sandbox__python_execute
#   mcp__sandbox__sandbox_config
```

### System Prompt 更新

Phase 4 — Data Exploration（重写）：
- 使用 `os.listdir` 查看目录结构
- 使用 `python_inspect` 快速探索数据（head/describe/info/value_counts）
- 使用 `python_execute` 跑多步分析脚本
- 迭代式探索：每个观察指导下一个问题

Phase 5 — Data Cleaning & EDA Report（重写）：
- 基于 Phase 4 发现设计清洗策略
- 使用 `python_execute` 执行清洗脚本
- 使用 `python_inspect` 验证清洗结果
- 编写 `eda.md`

不再包含防死循环的 prompt 规则（由 RunGuard 处理）。

### 文件变更清单

| 操作 | 文件 |
|---|---|
| **新增** | `src/athena/sandbox/__init__.py` |
| **新增** | `src/athena/sandbox/__main__.py` |
| **新增** | `src/athena/sandbox/server.py` |
| **新增** | `src/athena/sandbox/executor.py` |
| **新增** | `src/athena/sandbox/limits.py` |
| **新增** | `src/athena/sandbox/lifecycle.py` |
| **新增** | `src/athena/sandbox/whitelist.py` |
| **新增** | `src/athena/core/agent/guard.py` |
| **更新** | `src/athena/core/agent/runtime.py`（集成 RunGuard） |
| **更新** | `src/athena/core/agent/models.py`（AgentContext + guard） |
| **更新** | `src/athena/agents/competition/task_understand_agent.py`（删除旧 import，更新注释） |
| **更新** | `src/athena/prompts/competition/task_understand_system.txt`（重写 Phase 4-5） |
| **更新** | `mcp_servers.json`（新增 sandbox server 配置） |
| **删除** | `src/athena/tools/data_prepare.py` |
| **删除** | `src/athena/tools/_code_utils.py`（如无其他使用者） |

### MCP 配置

```json
{
  "name": "sandbox",
  "transport": "stdio",
  "command": "uv",
  "args": ["run", "python", "-m", "athena.sandbox.server"],
  "prefix": "mcp__sandbox__"
}
```

## 测试策略

### 第一层：SandboxExecutor 单元测试

`tests/test_sandbox_executor.py` — 不启动 MCP server，直接测核心：

- `test_inspect_basic` — `1+1` → `value: "2"`
- `test_inspect_dataframe` — DataFrame → 带 shape/columns
- `test_inspect_timeout` — 死循环 → 10s 超时
- `test_execute_script` — 写 CSV → 读回验证
- `test_execute_matplotlib` — `savefig` → 文件存在
- `test_import_whitelist_blocked` — `os.system(...)` → PermissionError
- `test_import_whitelist_allowed` — `pandas`/`numpy` → 通过
- `test_safe_os_listdir` — `os.listdir('.')` → 返回文件列表
- `test_safe_os_system_blocked` — `os.system(...)` → PermissionError
- `test_memory_limit` — 分配 >512MB → MemoryError
- `test_cwd_isolation` — 无法访问 `work_root/..`
- `test_result_truncation` — 超长输出 → 截断至 2000 字符

### 第二层：RunGuard 单元测试

`test/unit/agent_kernel/test_guard.py`：

- `test_duplicate_rejected` — 相同 `(tool, args)` 第二次 → GuardError
- `test_consecutive_failure_limit` — 连续 3 次 → GuardError
- `test_success_resets_failure_count` — fail,fail,success,fail → 不触发
- `test_error_pattern_limit` — 同一 pattern 3 次 → GuardError
- `test_different_args_allowed` — 同 tool 不同 args → 允许

### 第三层：MCP 集成测试

`test/integration/test_sandbox_mcp.py`：

- `test_mcp_discovery` — 启动 server → list_tools → 验证 3 个工具
- `test_mcp_python_inspect` — 通过 MCP 调用 → 正常结果
- `test_mcp_sandbox_config` — 返回白名单内容

### 第四层：TaskUnderstandAgent 端到端

`test/integration/test_task_understand_agent.py` — 更新以验证 Agent 能通过 Sandbox MCP 工具完成数据探索。

### 不测

- MCP JSON-RPC 协议解析（`mcp` 库的职责）
- `subprocess.run` 底层行为（Python 标准库的职责）
- LLM 是否按预期调用工具（prompt engineering，非单元测试关注点）

## 实施阶段

1. **Sandbox 核心**：`whitelist.py` → `limits.py` → `lifecycle.py` → `executor.py`（独立可测）
2. **MCP Server 包装**：`server.py` + `__main__.py`（手动连接验证）
3. **RunGuard**：`guard.py` + `runtime.py`/`models.py` 集成
4. **Agent 集成**：更新 `task_understand_agent.py` + system prompt + `mcp_servers.json`
5. **清理旧代码**：删除 `data_prepare.py`、`_code_utils.py`
6. **测试**：逐层测试覆盖
7. **端到端验证**：用 `demo_taskunderstand_agent.py` 跑真实数据集

# 通用 MCP 接入层设计（Kaggle 为首个接入方）

日期：2026-08-03
状态：已批准（brainstorming 三部分逐节确认）
分支：`TaskUnderstandingAgent`

## 1. 背景与目标

Athena 是一个自动化 AI4S 系统。现有 `TaskUnderstandAgent` 已注册 13 个竞赛工具，
其中 3 个 Kaggle 工具（`kaggle_competition_search` / `kaggle_discussion_search` /
`kaggle_dataset_download`）是 **stub**：`src/athena/tools/kaggle_search.py:18-24`
的 `_get_kaggle_mcp_client()` 直接 `raise NotImplementedError`，从未真正连通 Kaggle。

本次目标是构建一个**通用 MCP 接入层**，并让 **Kaggle 官方 MCP server
（`https://www.kaggle.com/mcp`）成为第一个接入方**。未来 Athena 可能面向任意领域
任务（数据需求不一定在 Kaggle 上），因此接入层必须支持：
- **零代码接入新 MCP server**：加一段配置即完成接入，不写 Python 工具类。
- **工具暴露可裁剪**：不把 MCP server 的全量工具清单塞给 LLM（token 与误选问题），
  采用 Codex 式"搜索激活"模式。

## 2. 现状分析（探索结论）

- **无任何 MCP 依赖/连接**：`pyproject.toml` 只有 `openai / pydantic / pydantic-ai /
  python-dotenv / pylatexenc / pymupdf / huggingface_hub`；无 `mcp`/`httpx`/`aiohttp`。
- **现有工具架构**：`BaseTool` 子类声明 `ToolSpec` + `async execute()`；
  `ToolRegistry` 构建期显式注册、按名排序（prompt-cache 稳定）；agent 循环
  （`agent.py:_sampling_loop`）按名 `resolve`，`concurrency_safe=False` 的工具被
  串行屏障调度。工具输出统一落盘到 `{work_root}/{tool_name}/`。
- **LLM 侧可见性**：`provider.py:47` 每轮从 `config.tools.specs` 重建 OpenAI 工具定义
  —— **运行期向注册表新增 spec，下一轮 LLM 请求即会看到**，这是"搜索激活"的机制基础。
- **无运行时 schema 校验**：`BaseTool._validate` 是 no-op，`input_schema` 只对 LLM 起引导作用。
- **13 个工具断言**：`task_understand_agent.py:125` `assert len(tools) == 13`。
- **.env 已有**：`KAGGLE_API_TOKEN=KGAT_...`（官方 MCP bearer token，`KGAT` 开头）。

## 3. 总体架构

```
mcp_servers.json / 构造注入
        │  McpServerConfig[]
        ▼
┌─────────────────────────────────────────────────────────┐
│ mcp_search_tools（BaseTool，全局唯一入口，常驻）            │
│   搜本地清单 → 命中 → 注册进 ToolRegistry → 返回精简 schema │
└───────────────┬─────────────────────────────────────────┘
                │ 管理 N 个 server
        ┌───────▼────────┐   lazily connect    ┌────────────────────┐
        │ McpClientManager│ ─────────────────► │ Kaggle MCP server  │
        │ per server      │  initialize/list   │ streamable_http    │
        │ manifest 缓存    │  tools/call_tool   │ https://.../mcp    │
        └───────┬────────┘                     └────────────────────┘
                │ 发现时生成
        ┌───────▼────────┐
        │ McpToolAdapter  │  BaseTool：schema 映射 / 结果归一化 /
        │ (per tool)      │  落盘 / 文件下载处理
        └────────────────┘
```

核心模块（新增 `src/athena/tools/mcp/` 子包，遵循现有工具目录风格）：

| 模块 | 职责 |
|---|---|
| `mcp/config.py` | `McpServerConfig` dataclass + JSON 加载器 |
| `mcp/client.py` | `McpClientManager`：连接、`list_tools`、`call_tool`、重连、manifest 缓存 |
| `mcp/adapter.py` | `McpToolAdapter(BaseTool)`：由 MCP 工具定义构建 |
| `mcp/search.py` | `McpSearchTools(BaseTool)`：全局搜索 + 激活注册 |
| `mcp/__init__.py` | `register_mcp_tools(...)` 装配入口 |

## 4. 工具暴露模型（Codex 式两层）

**常驻（每轮请求全量 schema，仅此两类）**
1. Athena 原生工具（`data_prepare` / `baseline_builder` / `hf_dataset` / `hf_model` /
   `_code_utils` 等）——Athena 自身能力，不受 MCP 影响。
2. 全局入口 `mcp_search_tools`——描述为"搜索所有已接入的外部 MCP 服务可用工具"。

**默认隐藏（不在首轮清单）**
- 所有 MCP 工具的 schema，**包括 Kaggle 相关的全部工具**
  （`kaggle__search_competitions`、`kaggle__download_competition_data_file` 等）。
  LLM 需要时先调 `mcp_search_tools`，命中 → 注册 → 下一轮可见可调。
- `pinned_tools` 配置保留但**默认空**，仅作"明确已知常用"的逃生舱。

> 注：文中出现的 `kaggle__search_competitions` / `kaggle__submit_competition` /
> `kaggle__download_competition_data_file` 等均为**示例工具名**。接入层的 schema 映射、
> 搜索、激活完全与名字无关，真实工具名以 `list_tools` 运行时返回为准；
> 配置里的 `pinned_tools` 也用真实名字填写。

**运行流程示例**
```
第 1 轮: LLM 只看到 [原生工具…, mcp_search_tools]
  → LLM 调用 mcp_search_tools {query: "提交竞赛结果"}
  → 返回 "kaggle__submit_competition" + 精简 schema，并注册进 ToolRegistry
第 2 轮: LLM 请求中出现 kaggle__submit_competition → 直接调用
```

**护栏**
- 单次运行发现工具数上限 `max_discovered`（默认 30），防止 prompt 无限膨胀。
- 重复注册直接跳过（防 KeyError）。
- 搜索结果只返回 top-K（默认 8）个**精简 schema**（属性名 + 类型，截断长描述）。

## 5. 配置结构

`McpServerConfig` dataclass，构建期注入（与 `work_root` 注入同一模式），并提供
JSON 加载器保证零代码体验：

```json
{
  "servers": [
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

- `transport`：`streamable_http`（远程，Kaggle 用）或 `stdio`（本地子进程，
  社区 server 常用）。`stdio` 时用 `command` + `args` 代替 `url`。
- `auth`：v1 只支持 **bearer token**，值从 `.env` 读（默认 `KAGGLE_API_TOKEN`，
  已配置）。OAuth 2.0 流程列为非目标。
- `tool_prefix`：防多 server 命名冲突。发现的工具名 = `{tool_prefix}{mcp_tool_name}`。
- `pinned_tools`：逃生舱，默认空。
- **未配置任何 server → 不注册搜索工具，接入层完全惰性，现有行为不变。**

## 6. 连接生命周期（默认全懒）

- **懒连接**：`pinned_tools=[]` 时 agent 构建 **0 网络请求**。首次调用
  `mcp_search_tools` 或首个被发现的工具时才建立连接 → `initialize` → `list_tools`
  → 缓存 manifest。搜索在内存 manifest 上匹配，无网络开销。
- **钉了工具才构建期连接**：若配置了 `pinned_tools`，构建时连接一次取这几个工具的
  schema，并保持连接供后续调用。
- **传输**：官方 `mcp` Python SDK —— `streamablehttp_client(url, headers)` /
  `stdio_client(command, args)` + `ClientSession`。SDK 原生 async，`execute()` 内直接
  `await`，**不需要 `asyncio.to_thread`**（比现有 HF 工具更干净）。
- **容错**：调用失败先重连一次 + 指数退避重试；单次调用超时默认 120s（可配）；
  manifest 缓存，连接断开不影响后续搜索。

## 7. 工具适配器 `McpToolAdapter`

### 7.1 Schema 映射
`tools/list` 返回的 `name` / `description` / `inputSchema`（已是 JSON Schema）映射到
`ToolSpec`：`name` 加 `tool_prefix`；`input_schema` 原样使用；`concurrency_safe=False`
（落盘 + 网络）；`max_result_chars` 使用默认值（循环本身有 50k 截断兜底）。
Athena `_validate` 是 no-op，无额外校验工作。

### 7.2 调用与结果归一化
`execute()` → `await call_tool(name, args)`，结果归一化：
- 文本 parts 拼接；
- `structuredContent`（若有）优先转为 JSON；
- 图片/`embedded_resource` 资源块 → 落盘保存。
- 返回给 LLM 的**精简摘要**：`output_dir` + 文件路径 + 简短预览，不把整个载荷塞入上下文。

### 7.3 落盘（通用层统一规则）
每次调用（成功与失败都写，沿用 3394de5 约定）写入
`{work_root}/mcp/{server}/{tool}/result.json`（固定文件名，可被下游工具引用），
`ToolResult.data` 恒带 `output_dir`。命名空间放在 `mcp/` 父目录下，不与原生工具目录
（如 `kaggle_competition_search`）冲突。

### 7.4 文件下载处理
`download_competition_data_file` 这类工具返回 `HttpRedirect`（重定向 URL），适配器
检测三种可下载形态：
1. `embedded_resource` 内 base64 blob → 直接解码落盘；
2. 结果中的 `url`/`download_url` 字段 → 用 httpx 跟随重定向流式下载；
3. 工具名匹配下载模式（`download`/`get_file`…，可配置）→ 同上。

**安全护栏**
- 只跟随工具自身返回的 URL，**绝不**跟随 LLM 提供的 URL；
- 下载文件只允许写入该工具管理的输出目录（LLM 提供的 `output_dir` 一律忽略，
  与现有 hf/kaggle 工具行为一致）；
- token 只在 header 中传递、从不打日志。

## 8. 搜索工具 `mcp_search_tools`

- 参数：`query`（必填）、可选 `server` 过滤、可选 `top_k`（默认 8）。
- 在全部已配置 server 的缓存 manifest 上做关键字/相关性匹配（name + description）。
- 命中即**注册进 ToolRegistry**（已注册则跳过）；`provider.py` 每轮从
  `config.tools.specs` 重建工具定义，因此下一轮 LLM 请求即出现该工具的 schema。
- 返回精简 schema（属性名 + 类型），受 `max_discovered`（默认 30）上限约束。

## 9. 与现有 agent 集成

- **删除** `src/athena/tools/kaggle_search.py`（3 个 stub）及
  `test/unit/tools/test_kaggle_search.py` —— 全部 Kaggle 能力改由 MCP 自动发现提供。
- **系统提示词**：`task_understand_agent.py` 中"总是先调 `kaggle_competition_search`"
  改为"需要外部数据/服务时，先用 `mcp_search_tools` 搜索可用工具"。
- **断言**：`len(tools) == 13` 改为只对**原生工具**计数断言（MCP 工具动态，不参与）。
- `build_task_understand_agent` 增加可选 `mcp_servers` 参数；未配置时接入层不生效。

## 10. 错误处理

- 首次搜索连接失败：返回 `ToolResult(success=False, error=...)`，信息包含 server 名
  与缺失配置项；agent 循环无自动重试，靠 LLM ReAct 重试（与现有约定一致）。
- `call_tool` 返回 `isError=true` 或抛异常：由 `BaseTool._execute` 包装为
  `ToolResult(success=False, error=...)`，错误结果同样落盘。
- 认证缺失：报错明确指出缺失的 env 变量名。
- 超时/断线：重连一次 + 指数退避，仍失败则返回错误。

## 11. 测试策略

- **通用层单测**（`test/unit/tools/mcp/`）：用 MCP SDK 起**内存假 server**
  （含搜索类工具 + 一个假下载工具），断言：schema 映射、搜索匹配 + 激活注册、
  call_tool 归一化、落盘（result.json）、下载文件落盘、错误包装。全程无网络。
- **Kaggle 真连冒烟测试**（`test/integration/`）：由 `KAGGLE_API_TOKEN` 门控
  （有 token 才跑），复刻现有 OpenAlex key 的 gating 模式。
- 原生工具测试不动；kaggle stub 测试删除。

## 12. 依赖变更

`pyproject.toml` 增加 `mcp`、`httpx`（streamable_http 传输需要）。
**不引入** `kaggle` / `kagglehub` —— 所有能力走 MCP server 侧。

## 13. 明确不做（非目标）

- OAuth 2.0 授权流程（v1 仅 bearer token）；
- 运行期动态移除工具（registry 在一次运行中只增不减）；
- MCP `resources` / `prompts` 暴露（只接 `tools`）；
- 服务端 `tools/search`（客户端在缓存上匹配更可移植）；
- 基于使用频率的自动钉住；
- 直连 `kaggle` / `kagglehub`。

## 15. 开发约定

- 开发过程中的 **review、report 等文档一律使用中文**。
- **commit 消息使用中文**（沿用现有提交历史风格：`feat:` / `fix:` / `docs:` 前缀 + 中文描述）。
- **代码遵循 `docs/代码规范.md`**，要点：
  - 导入全部置于文件开头，顺序为标准库 → 第三方 → 项目内；不使用
    `from __future__ import annotations`；`TYPE_CHECKING` 仅用于避免循环导入。
  - 公开函数/方法/类必须有 docstring，描述**做什么**（非怎么做）；超过 15 行的函数
    在关键阶段前加单行注释；禁止 ASCII 装饰分隔线。
  - `except` 捕获特定异常时须注释触发场景（如 `asyncio.CancelledError` → 取消信号）；
    禁止裸 `except:`。
  - 空行规范：导入与代码、类/函数定义、逻辑分组之间各一个空行，禁止连续两个空行。
  - 代码嵌套不超过 3 层，简单优于复杂，不引入无必要的模式/封装/依赖。
  - 每个模块配有单元测试，公开函数在 docstring 或测试中给出简单输入输出示例。

## 16. 参考

- Kaggle 官方 MCP server：https://www.kaggle.com/mcp （文档 https://www.kaggle.com/docs/mcp）
  - 认证：OAuth 2.0 或 bearer token（`KGAT` 开头，Kaggle 设置页生成）
  - 覆盖 Notebooks / Competitions / Datasets / Models / Benchmarking
- 现有相关代码：
  - `src/athena/core/tool.py`（`BaseTool` / `ToolRegistry`）
  - `src/athena/core/tool_types.py`（`ToolSpec` / `ToolResult`）
  - `src/athena/core/agent/agent.py`（ReAct 循环、串行屏障）
  - `src/athena/core/agent/provider.py`（每轮重建工具定义）
  - `src/athena/agents/competition/task_understand_agent.py`（注册 + 13 断言 + 提示词）
  - `src/athena/tools/kaggle_search.py`（待删除的 3 个 stub）

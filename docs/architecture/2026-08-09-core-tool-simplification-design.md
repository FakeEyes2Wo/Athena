# Core 工具设计极简改造设计

> 2026-08-09 · 分支 `feat/prompt-driven-agents` · 参考 Pi agent（极简编码 agent：只保留
> read/write/edit/bash 四个工具、最小核心、"不需要的东西就不存在"）

## 背景

当前工具系统有两个问题：

1. **工具数量偏多**：src 下存在 `write_script`/`run_script`/`commit_result`
   三个从未被任何调用方使用的遗留工具（`script_tools.py`），污染工具面。
2. **工具定义繁琐**：每个工具都要写一个 `BaseTool` 子类 + `spec` + `execute`，
   一个简单工具约 40-50 行。`@tool` 装饰器虽已存在，但必须显式给出
   `input_schema`，且 src 中零使用。

目标：向 Pi agent 的极简哲学靠拢 —— 通用工具集对齐 Pi 的 **4 个工具**，工具
定义简化为 **一行 `@tool` 装饰函数**。

方向已与用户确认：

1. **通用工具集对齐 Pi 4 件套**：`read_file`/`write_file`/`bash`/`pwsh`（**pwsh
   保留**，Windows 环境需要）。
2. **`@tool` 保留并增强**，使其"一行直接把函数设定为 tool"：裸用 + 自动推导
   `name`/`description`/`input_schema`；`description` 取**完整 docstring**。
3. **删除未使用的脚本工具** `write_script`/`run_script`/`commit_result`。
4. **其他不动**：`ToolSpec`/`ToolResult`/`ToolContext`/`BaseTool`/`ToolRegistry`
   结构、runtime、交互/编排/论文域工具均保持现状。

## 目标架构

```
@tool
async def read_file(path: str, start_line: int | None = None, ...) -> dict:
    """完整 docstring → tool.description"""
    ...   # 闭包捕获 workspace；name = 函数名；schema 由类型注解生成

generic_tool_registry(workspace)  →  ToolRegistry[read_file, write_file, bash, pwsh]
```

`@tool` 仍返回 `BaseTool` 实例，因此 `ToolRegistry`/`Agent`/runtime 的消费路径
**零改动** —— 这是本设计"其他不管"的落点。

## 改动清单

### 1. `core/tool.py` — 增强 `@tool`（核心改动）

- 支持两种用法：裸用 `@tool`（无括号）与带参 `@tool(name=..., description=...,
  input_schema=...)`；`name` 参数为可调用对象时判定为裸用。
- 自动推导（未显式给定）：
  - `name` ← 函数名
  - `description` ← **完整 docstring**（`fn.__doc__.strip()`，非首行）
  - `input_schema` ← `inspect.signature` 类型注解 → JSON Schema
- 类型映射（小助手函数，置于 `core/tool.py`）：
  - `str`→`{"type":"string"}`、`int`→`{"type":"integer"}`、
    `float`→`{"type":"number"}`、`bool`→`{"type":"boolean"}`、
    `list[X]`→`{"type":"array","items":{...}}`、`dict`→`{"type":"object"}`
  - 有默认值 → 可选（不入 `required`，默认值写入 schema `default`）；
    无默认值 → 必填
- 行为保持：仍返回 `BaseTool` 实例；`execute` 仍是 `await fn(**input)`；
  函数返回原始数据由 `_execute` 包装为 `ToolResult`。

### 2. `agents/tools/generic_tools.py` — 四个工具改写为一行 `@tool`

- `read_file` / `write_file` / `bash` / `pwsh` 各自用 `@tool` 装饰的异步函数，
  在 `generic_tool_registry(workspace)` 内闭包捕获 `workspace`（`_workspace_path`
  逃逸防护、`_shell_env` 保留）。
- `bash`/`pwsh` 共用一个内部实现（shell/flag 由闭包参数化），命名推导或用
  `@tool(name=...)` 显式命名。
- 错误返回从 `ToolResult(data=None, success=False, error=...)` 改为 **`raise`**
  （`_execute` 统一捕获为 `ToolResult(success=False)`），更简洁。

### 3. 删除未使用的脚本工具

- 删除 `agents/tools/script_tools.py`。
- `agents/tools/__init__.py` 清空导出（grep 确认无调用方 import 这三个工具）。
- 删除 `test/unit/agents/test_script_tools.py`。
- 确认 `data_agent.py`/`project_runtime.py`/`supervisor.py` 无引用（已 grep
  确认仅 `tools/__init__.py` 引用）。

### 4. 测试

- `test/unit/test_tool.py`：补 `@tool` 裸用、完整 docstring 为 description、
  schema 自动推导（必填/可选/默认值/list 类型）用例。
- `test/unit/agents/test_generic_tools.py`：适配 `@tool` 新写法（用
  `generic_tool_registry` 或直接构造闭包工具）。
- 跑全套单测，确认无回归。

## 不做的事（YAGNI / 用户明确"暂时不管"）

- 不删 `pwsh`；不加 `edit_file`（数量已与 Pi 一致 = 4）。
- 不改 `ToolSpec`/`ToolResult`/`ToolContext`/`ToolRegistry`/`BaseTool` 结构。
- 不改 runtime / `request_user_input` / 编排工具（`spawn`/`send`/`followup`/
  `wait_for`/`wait_for_human`）/ 论文域工具。
- `@tool` 的 `ctx: ToolContext` 注入（方案 B）记为后续扩展，不在本次范围。

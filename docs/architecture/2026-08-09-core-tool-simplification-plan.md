# Core 工具设计极简改造实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `@tool` 装饰器支持"一行把函数设定为 tool"（裸用 + 自动推导 name/完整 docstring/类型注解 schema），并把通用工具集对齐 Pi 的 4 件套（read_file/write_file/bash/pwsh），删除未使用的脚本工具。

**Architecture:** `@tool` 仍返回 `BaseTool` 实例，因此 `ToolSpec`/`ToolResult`/`ToolContext`/`ToolRegistry`/runtime 消费路径零改动。`generic_tools.py` 用闭包捕获 workspace，四个工具各写成一行 `@tool` 异步函数。

**Tech Stack:** Python ≥ 3.11，pytest-asyncio（`asyncio_mode = "auto"`，async 测试无需装饰器），black line-length 88。

**Spec:** `docs/architecture/2026-08-09-core-tool-simplification-design.md`

## Global Constraints

- 工具集固定为 4 个：`read_file` / `write_file` / `bash` / `pwsh`（**pwsh 保留，不加 edit_file**）。
- `@tool` 的 `description` 默认取**完整 docstring**（非首行）。
- 不改 `ToolSpec` / `ToolResult` / `ToolContext` / `BaseTool` / `ToolRegistry` 结构，不改 runtime / `request_user_input` / 编排工具 / 论文域工具。
- `generic_tool_registry(workspace: Path) -> ToolRegistry` 接口签名不变（src 消费方 `prompt_agent.py`、`test/unit/_support.py`、`test/unit/agent/test_data_agent.py` 依赖它）。
- `pytest` 命令：Windows 下用 `python -m pytest`；慢/API 测试用 `-m "not slow"` 排除。
- 错误返回统一 `raise`（由 `_execute` 捕获为 `ToolResult(success=False)`）。

---

### Task 1: 增强 `@tool` — 裸用 + 完整 docstring + 自动 schema

**Files:**
- Modify: `src/athena/core/tool.py`
- Test: `test/unit/test_tool.py`

**Interfaces:**
- Consumes: 现有 `BaseTool` / `ToolSpec` / `ToolContext`（`core/tool_types.py`）。
- Produces: `tool(...)` 装饰器 — 支持裸用 `@tool` 与带参 `@tool(name=..., description=..., input_schema=...)`；未显式给的 name/description/input_schema 分别由函数名、完整 docstring、类型注解推导；返回 `BaseTool` 实例。内部助手 `_schema_from_signature(fn) -> dict` / `_type_to_schema(annotation) -> dict`。

- [ ] **Step 1: 在 `test/unit/test_tool.py` 追加失败测试**

在文件末尾（`TestToolRegistry` 之后）追加：

```python
@tool
async def _bare_echo(text: str, n: int = 2) -> dict:
    """完整 docstring 作为 description。

    第二行保留。
    """
    return {"text": text, "n": n}


class TestToolAutoDerive:
    def test_bare_tool_returns_base_tool(self):
        assert isinstance(_bare_echo, BaseTool)

    def test_bare_tool_derives_name(self):
        assert _bare_echo.spec.name == "_bare_echo"

    def test_bare_tool_full_docstring_description(self):
        assert (
            _bare_echo.spec.description
            == "完整 docstring 作为 description。\n\n第二行保留。"
        )

    def test_bare_tool_derives_schema(self):
        schema = _bare_echo.spec.input_schema
        assert schema["type"] == "object"
        assert schema["required"] == ["text"]
        assert schema["properties"]["text"] == {"type": "string"}
        assert schema["properties"]["n"] == {"type": "integer", "default": 2}

    def test_optional_union_param_not_required(self):
        @tool
        async def _opt(path: str, start: int | None = None) -> dict:
            """docstring."""
            return {}

        schema = _opt.spec.input_schema
        assert schema["required"] == ["path"]
        assert schema["properties"]["start"] == {"type": "integer"}

    def test_decorated_tool_runs(self):
        result = _bare_echo.invoke(text="hi")
        assert result.success
        assert result.data == {"text": "hi", "n": 2}
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest test/unit/test_tool.py -v -k ToolAutoDerive`
Expected: FAIL — `_bare_echo.spec.name` 是函数对象而非 `"_bare_echo"`（当前裸用 `@tool` 会把函数当 `name` 参数传入）。

- [ ] **Step 3: 实现新 `@tool` + schema 助手**

修改 `src/athena/core/tool.py`：

1) 扩展导入（第 4-5 行处）：

```python
import asyncio
import inspect
import traceback
import types
from abc import ABC, abstractmethod
from typing import Any, Callable, Union, get_args, get_origin
```

2) 在 `tool` 装饰器前新增两个助手函数：

```python
def _type_to_schema(annotation: Any) -> dict:
    """Python 类型注解 → JSON Schema 片段（str/int/float/bool/list/dict/Optional）。

    无法识别/未注解 → 空 dict（不约束该参数）。
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return _type_to_schema(args[0]) if args else {}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if origin is list or annotation is list:
        items = get_args(annotation)
        return {"type": "array", "items": _type_to_schema(items[0]) if items else {}}
    if annotation is dict or origin is dict:
        return {"type": "object"}
    return {}


def _schema_from_signature(fn: Any) -> dict:
    """由函数签名生成 JSON Schema：无默认值 → required；非 None 默认值 → default。

    ``*args``/``**kwargs`` 忽略；``X | None = None`` 视为可选且不写 default。
    """
    props: dict[str, dict] = {}
    required: list[str] = []
    for pname, p in inspect.signature(fn).parameters.items():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        prop = _type_to_schema(p.annotation)
        if p.default is not inspect.Parameter.empty:
            if p.default is not None:
                prop["default"] = p.default
        else:
            required.append(pname)
        props[pname] = prop
    return {"type": "object", "properties": props, "required": required}
```

3) 整体替换 `tool` 函数（原 73-102 行）：

```python
def tool(
    _fn: Any | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    input_schema: dict | None = None,
    **spec_kwargs: Any,
) -> Any:
    """装饰器：把异步函数转成 ``BaseTool`` 实例。

    支持裸用 ``@tool`` 与带参 ``@tool(name=..., description=..., input_schema=...)``
    两种写法。未显式给出的 name/description/input_schema 分别由函数名、**完整
    docstring**、类型注解自动推导。
    """

    def deco(fn: Any) -> BaseTool:
        spec_name = name or fn.__name__
        doc = (fn.__doc__ or "").strip()
        spec_desc = description if description is not None else (doc or fn.__name__)
        schema = (
            input_schema if input_schema is not None else _schema_from_signature(fn)
        )

        class _T(BaseTool):
            spec = ToolSpec(
                name=spec_name,
                description=spec_desc,
                input_schema=schema,
                **spec_kwargs,
            )

            async def execute(self, input: dict, ctx: ToolContext) -> Any:
                return await fn(**input)

        _T.__name__ = fn.__name__
        _T.__qualname__ = fn.__qualname__
        _T.__doc__ = fn.__doc__
        return _T()

    return deco(_fn) if _fn is not None else deco
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest test/unit/test_tool.py -v`
Expected: PASS — 原有 `TestBaseTool`/`TestToolDecorator`/`TestToolRegistry` 与新 `TestToolAutoDerive` 全部通过。

- [ ] **Step 5: Commit**

```bash
git add test/unit/test_tool.py src/athena/core/tool.py
git commit -m "feat: @tool bare usage with full-docstring description and auto schema"
```

---

### Task 2: `generic_tools.py` 改写为一行 `@tool` 闭包

**Files:**
- Rewrite: `src/athena/agents/tools/generic_tools.py`
- Rewrite: `test/unit/agent/test_generic_tools.py`

**Interfaces:**
- Consumes: `@tool`（Task 1）。
- Produces: `generic_tool_registry(workspace: Path) -> ToolRegistry`（签名不变），注册 `read_file` / `write_file` / `bash` / `pwsh` 四个工具，spec 名称与输入字段不变；错误改为 `raise`（`_execute` 包装为 `ToolResult(success=False)`，error 文本含异常类型名如 `FileNotFoundError`）。

- [ ] **Step 1: 重写测试文件**

整体替换 `test/unit/agent/test_generic_tools.py`：

```python
# test/unit/agent/test_generic_tools.py
import asyncio
from pathlib import Path

from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.tool_types import ToolContext


def _ctx() -> ToolContext:
    return ToolContext("t", "id", lambda *a: asyncio.sleep(0), asyncio.Event())


async def test_read_file_line_range(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 6)), encoding="utf-8"
    )
    tool = generic_tool_registry(tmp_path).resolve("read_file")
    result = await tool.ainvoke(_ctx(), path="a.txt", start_line=2, end_line=3)
    assert result.data["content"] == "line2\nline3"


async def test_write_file_creates_file(tmp_path: Path) -> None:
    tool = generic_tool_registry(tmp_path).resolve("write_file")
    result = await tool.ainvoke(_ctx(), path="sub/b.txt", content="hello")
    assert result.success
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "hello"


async def test_path_escape_is_rejected(tmp_path: Path) -> None:
    tool = generic_tool_registry(tmp_path).resolve("write_file")
    result = await tool.ainvoke(_ctx(), path="../evil.txt", content="x")
    assert not result.success
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_bash_runs_and_returns_output(tmp_path: Path) -> None:
    tool = generic_tool_registry(tmp_path).resolve("bash")
    result = await tool.ainvoke(_ctx(), command="echo hello-from-bash")
    assert result.success
    assert "hello-from-bash" in result.data["stdout"]


async def test_read_file_missing_raises(tmp_path: Path) -> None:
    """缺失文件 → 抛 FileNotFoundError → ToolResult(success=False)。"""
    tool = generic_tool_registry(tmp_path).resolve("read_file")
    result = await tool.ainvoke(_ctx(), path="nope.txt")
    assert not result.success
    assert "FileNotFoundError" in result.error


async def test_registry_has_four_tools(tmp_path: Path) -> None:
    reg = generic_tool_registry(tmp_path)
    assert {t.name for t in reg.specs} == {"read_file", "write_file", "bash", "pwsh"}
```

- [ ] **Step 2: 运行确认 `test_read_file_missing_raises` 失败**

Run: `python -m pytest test/unit/agent/test_generic_tools.py -v`
Expected: 除 `test_read_file_missing_raises` 外全 PASS；该用例 FAIL — 旧实现 `read_file` 缺失时返回 `error="file not found: ..."`，不含 `"FileNotFoundError"`。

- [ ] **Step 3: 重写 `generic_tools.py`**

整体替换 `src/athena/agents/tools/generic_tools.py`：

```python
"""通用文件/命令工具集（对齐 Pi：read_file / write_file / bash / pwsh）。

沙箱约定：``read_file``/``write_file`` 限定 workspace 内（路径逃逸防护）；
``bash``/``pwsh`` 以 workspace 为 cwd 执行。Python 脚本经 ``bash("python x.py")``
覆盖。四个工具由 ``generic_tool_registry`` 用闭包捕获 workspace，一行 ``@tool``
定义。
"""

import asyncio
import os
from pathlib import Path

from athena.core.tool import ToolRegistry, tool

_HostAllow = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


def _workspace_path(root: Path, path: str) -> Path:
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


def _shell_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _HostAllow}


async def _run_command(
    root: Path, shell: str, flag: str, command: str, timeout_s: int
) -> dict:
    proc = await asyncio.create_subprocess_exec(
        shell,
        flag,
        command,
        cwd=str(root.resolve()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_shell_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"TIMEOUT after {timeout_s}s") from exc
    return {
        "returncode": proc.returncode or 0,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def generic_tool_registry(workspace: Path) -> ToolRegistry:
    """构造对齐 Pi 的最小通用工具集：read_file / write_file / bash / pwsh。"""
    root = workspace.resolve()

    @tool
    async def read_file(
        path: str, start_line: int | None = None, end_line: int | None = None
    ) -> dict:
        """Read a file, optionally within a 1-based line range [start_line, end_line]."""
        path_obj = _workspace_path(root, path)
        if not path_obj.is_file():
            raise FileNotFoundError(path_obj)
        lines = path_obj.read_text(encoding="utf-8").splitlines()
        if start_line is None and end_line is None:
            content = "\n".join(lines)
        else:
            content = "\n".join(lines[(start_line or 1) - 1 : end_line or len(lines)])
        return {"path": str(path_obj), "content": content}

    @tool
    async def write_file(path: str, content: str) -> dict:
        """Create or overwrite a file in the workspace."""
        path_obj = _workspace_path(root, path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(content, encoding="utf-8")
        return {"path": str(path_obj)}

    def _command_tool(shell: str, flag: str, name: str, description: str):
        @tool(name=name, description=description)
        async def _cmd(command: str, timeout_s: int = 120) -> dict:
            """Run a shell command in the workspace."""
            return await _run_command(root, shell, flag, command, timeout_s)

        return _cmd

    reg = ToolRegistry()
    reg.register(read_file)
    reg.register(write_file)
    reg.register(
        _command_tool(
            "bash",
            "-c",
            "bash",
            "Run a bash command in the workspace and return stdout/stderr/returncode",
        )
    )
    reg.register(
        _command_tool(
            "pwsh",
            "-Command",
            "pwsh",
            "Run a PowerShell command in the workspace and return stdout/stderr/returncode",
        )
    )
    return reg
```

- [ ] **Step 4: 运行确认全部通过**

Run: `python -m pytest test/unit/agent/test_generic_tools.py -v`
Expected: PASS — 包括 `test_read_file_missing_raises`。

- [ ] **Step 5: 回归依赖方**

Run: `python -m pytest test/unit/_support.py test/unit/agent/test_data_agent.py test/unit/agent/test_builtin_agents.py -q -m "not slow"`
Expected: PASS — `generic_tool_registry` 接口未变，消费方不受影响。

- [ ] **Step 6: Commit**

```bash
git add src/athena/agents/tools/generic_tools.py test/unit/agent/test_generic_tools.py
git commit -m "refactor: generic tools as one-line @tool closures (Pi-aligned, keep pwsh)"
```

---

### Task 3: 删除未使用的脚本工具

**Files:**
- Delete: `src/athena/agents/tools/script_tools.py`
- Delete: `test/unit/agents/test_script_tools.py`
- Modify: `src/athena/agents/tools/__init__.py`

**Interfaces:**
- Consumes: 无（grep 已确认 `WriteScriptTool`/`RunScriptTool`/`CommitResultTool` 仅在自身文件、`__init__.py` 与 `test_script_tools.py` 中被引用）。
- Produces: 无。

- [ ] **Step 1: 删除文件并清空 `__init__.py` 导出**

```bash
git rm src/athena/agents/tools/script_tools.py test/unit/agents/test_script_tools.py
```

将 `src/athena/agents/tools/__init__.py` 整体替换为：

```python
"""Agent 业务工具集（通用工具见 generic_tools.py）。"""
```

- [ ] **Step 2: 全量回归**

Run: `python -m pytest test -q -m "not slow"`
Expected: PASS — 无任何模块引用已删除的脚本工具。

- [ ] **Step 3: Commit**

```bash
git add -A src/athena/agents/tools test/unit/agents
git commit -m "chore: remove unused script tools (write_script/run_script/commit_result)"
```

---

## Self-Review

**1. Spec 覆盖检查：**
- [x] `@tool` 裸用 + 自动推导 name/description/schema → Task 1
- [x] description 取完整 docstring → Task 1 Step 3（`spec_desc = ... else (doc or fn.__name__)`）
- [x] 通用工具对齐 Pi 4 件套（pwsh 保留，不加 edit）→ Task 2（4 个工具，`test_registry_has_four_tools` 断言）
- [x] 删除 `write_script`/`run_script`/`commit_result` → Task 3
- [x] ToolSpec/ToolResult/ToolContext/BaseTool/ToolRegistry/runtime 零改动 → Global Constraints（Task 1 只改 `tool` 装饰器本身，返回类型仍为 BaseTool）

**2. 占位符扫描：** 所有步骤含完整代码与命令，无 TBD/TODO。

**3. 类型一致性：**
- `generic_tool_registry(workspace: Path) -> ToolRegistry` 在 Task 2 定义，Task 2 Step 5 回归消费方签名一致。
- `@tool` 返回 BaseTool 实例 → `reg.register(read_file)` 合法（`ToolRegistry.register(t: BaseTool)`）。
- `_schema_from_signature` 生成的 `{"type":"object","properties":{...},"required":[...]}` 与 `ToolSpec.input_schema: dict` 匹配。

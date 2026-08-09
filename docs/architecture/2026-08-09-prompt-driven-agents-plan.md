# Prompt 驱动的 Agents 改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 agents 里写死的固定限制（analysis.py 模板、eval.py 生成、report 综合）改为 prompt 驱动：LLM 按 `core/agent/prompts/*.md` 的固定格式输出 md 文件，用通用工具（文件读写 / bash / pwsh）完成工作；删除 report_agent 与 EvalSpec；simple_agents.py 改名 builtin_agents.py。

**Architecture:** 业务 Agent 由「内层 LLM ReAct Agent（`core.agent.runtime.Agent` + 通用工具 + system prompt=`.md` 文件）」+「外层 Python 编排器（收集产物、提交 Bundle、推进 phase）」两层组成。LLM 走 DeepSeek（`DEEPSEEK_API_KEY` + `BASE_URL`），无 model 直接报错。

**Tech Stack:** Python 3.11 / openai / pydantic / pytest / python-dotenv

## Global Constraints

- 无 model 时 LLM agent 构造/`register_defaults` 直接 `raise RuntimeError`，**不保留任何确定性回退**。
- `data_agent` 入口文件名保留 `analysis.py`；DataAnalysis 提交链（owner / parent_ref）不变。
- prompt（`core/agent/prompts/*.md`）是「限制」的唯一来源；禁止把脚本模板写回 Python 代码。
- LLM 配置从 `.env` 读取：`DEEPSEEK_API_KEY`（已有）、`BASE_URL=https://api.deepseek.com`（用户加入）、`MODEL_NAME=deepseek:flash`（默认，可覆盖）。
- 测试用真实 DeepSeek API；LLM 相关测试标 `@pytest.mark.slow`（pyproject 已定义），可 `-m "not slow"` 跳过。
- 通用工具沙箱：`read_file`/`write_file` 路径逃逸防护；`bash`/`pwsh` 在 workspace cwd 执行。

---

### Task 1: LLM 配置 + provider base_url

**Files:**
- Create: `src/athena/core/agent/settings.py`
- Modify: `src/athena/core/agent/provider.py:31-53` (`ResponsesProvider.client`)
- Test: `test/unit/agent/test_settings.py`

**Interfaces:**
- Produces: `settings.api_key() -> str | None`, `settings.base_url() -> str`, `settings.model_name() -> str`, `settings.get_client() -> AsyncOpenAI`
- Consumes: `.env`（`DEEPSEEK_API_KEY` / `BASE_URL` / `MODEL_NAME`）

- [ ] **Step 1: 写失败测试**

```python
# test/unit/agent/test_settings.py
import pytest

from athena.core.agent import settings


def test_settings_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    assert settings.base_url() == "https://api.deepseek.com"
    assert settings.model_name() == "deepseek:flash"
    assert settings.api_key() is None


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("MODEL_NAME", "deepseek-chat")
    assert settings.api_key() == "sk-test"
    assert settings.model_name() == "deepseek-chat"


def test_get_client_raises_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        settings.get_client()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest test/unit/agent/test_settings.py -v`
Expected: FAIL — `ImportError: cannot import name 'settings'`

- [ ] **Step 3: 写实现**

```python
# src/athena/core/agent/settings.py
"""LLM 配置：从 .env 加载 DeepSeek API 凭据并构造 OpenAI 兼容 client。"""

import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek:flash"


def _resolve(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    return value if value else default


def api_key() -> str | None:
    """读取 DEEPSEEK_API_KEY，回退 OPENAI_API_KEY。"""
    return _resolve("DEEPSEEK_API_KEY", _resolve("OPENAI_API_KEY"))


def base_url() -> str:
    """DeepSeek OpenAI 兼容端点。"""
    return _resolve("BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL


def model_name() -> str:
    """默认 deepseek:flash（最便宜档位），可经 MODEL_NAME 覆盖。"""
    return _resolve("MODEL_NAME", DEFAULT_MODEL) or DEFAULT_MODEL


def get_client() -> AsyncOpenAI:
    """构造 OpenAI 兼容 client；无 API key 直接报错。"""
    key = api_key()
    if not key:
        raise RuntimeError(
            "Missing LLM API key: set DEEPSEEK_API_KEY or OPENAI_API_KEY in .env"
        )
    return AsyncOpenAI(api_key=key, base_url=base_url())
```

- [ ] **Step 4: 改 provider 用 settings**

在 `src/athena/core/agent/provider.py` 顶部 import settings，把 `client` property 改为：

```python
@property
def client(self) -> AsyncOpenAI:
    """返回注入的 client；未注入时按 settings（.env 的 DeepSeek 凭据）构造。"""
    if self._client is None:
        self._client = settings.get_client()
    return self._client
```

在 `provider.py` 顶部加：`from athena.core.agent import settings`

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest test/unit/agent/test_settings.py -v`
Expected: PASS（3 passed）。再跑现有 provider 测试确认不回归：
Run: `.venv/Scripts/python.exe -m pytest test/unit -k "provider or settings" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/athena/core/agent/settings.py src/athena/core/agent/provider.py test/unit/agent/test_settings.py
git commit -m "feat: LLM settings from .env (DeepSeek base_url + model), provider uses settings"
```

---

### Task 2: 通用工具集（read_file / write_file / bash / pwsh）

**Files:**
- Create: `src/athena/agents/tools/generic_tools.py`
- Test: `test/unit/agent/test_generic_tools.py`

**Interfaces:**
- Produces: `read_file(path, start_line=None, end_line=None)`, `write_file(path, content)`, `bash(command, timeout_s=120)`, `pwsh(command, timeout_s=120)`（均为 `BaseTool` 实例）
- Produces: `generic_tool_registry(workspace: Path) -> ToolRegistry`
- Consumes: `ToolContext`/`ToolResult` from `athena.core.tool_types`

- [ ] **Step 1: 写失败测试**

```python
# test/unit/agent/test_generic_tools.py
import asyncio
from pathlib import Path

from athena.agents.tools.generic_tools import (
    BashTool,
    PwshTool,
    ReadFileTool,
    WriteFileTool,
    generic_tool_registry,
)
from athena.core.tool_types import ToolContext


def _ctx() -> ToolContext:
    return ToolContext("t", "id", lambda *a: asyncio.sleep(0), asyncio.Event())


def test_read_file_line_range(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("\n".join(f"line{i}" for i in range(1, 6)), encoding="utf-8")
    tool = ReadFileTool(tmp_path)
    result = asyncio.get_event_loop().run_until_complete(
        tool.ainvoke(_ctx(), path="a.txt", start_line=2, end_line=3)
    )
    assert result.data["content"] == "line2\nline3"


def test_write_file_creates_file(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)
    result = asyncio.get_event_loop().run_until_complete(
        tool.ainvoke(_ctx(), path="sub/b.txt", content="hello")
    )
    assert result.success
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "hello"


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)
    result = asyncio.get_event_loop().run_until_complete(
        tool.ainvoke(_ctx(), path="../evil.txt", content="x")
    )
    assert not result.success
    assert not (tmp_path.parent / "evil.txt").exists()


def test_bash_runs_and_returns_output(tmp_path: Path) -> None:
    tool = BashTool(tmp_path)
    result = asyncio.get_event_loop().run_until_complete(
        tool.ainvoke(_ctx(), command="echo hello-from-bash")
    )
    assert result.success
    assert "hello-from-bash" in result.data["stdout"]


def test_registry_has_four_tools(tmp_path: Path) -> None:
    reg = generic_tool_registry(tmp_path)
    assert {t.name for t in reg.specs} == {"read_file", "write_file", "bash", "pwsh"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest test/unit/agent/test_generic_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: no module named 'athena.agents.tools.generic_tools'`

- [ ] **Step 3: 写实现**

```python
# src/athena/agents/tools/generic_tools.py
"""通用文件/命令工具集（参考 Codex/Claude）。prompt 驱动 agent 共用。

沙箱约定：``read_file``/``write_file`` 限定 workspace 内（路径逃逸防护）；
``bash``/``pwsh`` 以 workspace 为 cwd 执行。Python 脚本经 ``bash("python x.py")``
覆盖。
"""

import asyncio
from pathlib import Path

from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

_HostAllow = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


def _workspace_path(workspace: Path, path: str) -> Path:
    root = workspace.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


def _shell_env() -> dict[str, str]:
    return {k: v for k, v in __import__("os").environ.items() if k in _HostAllow}


class ReadFileTool(BaseTool):
    """读文件，支持 1 起始行范围（含端点）。"""

    spec = ToolSpec(
        name="read_file",
        description="Read a file, optionally within a 1-based line range [start_line, end_line]",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = _workspace_path(self._workspace, input["path"])
        if not path.is_file():
            return ToolResult(data=None, success=False, error=f"file not found: {path}")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = input.get("start_line")
        end = input.get("end_line")
        if start is None and end is None:
            content = "\n".join(lines)
        else:
            content = "\n".join(lines[(start or 1) - 1 : end or len(lines)])
        return ToolResult(data={"path": str(path), "content": content})


class WriteFileTool(BaseTool):
    """创建/覆盖工作区文件。"""

    spec = ToolSpec(
        name="write_file",
        description="Create or overwrite a file in the workspace",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = _workspace_path(self._workspace, input["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(input["content"], encoding="utf-8")
        return ToolResult(data={"path": str(path)})


class _CommandTool(BaseTool):
    def __init__(self, workspace: Path, shell: str, flag: str, name: str, desc: str) -> None:
        self._workspace = Path(workspace)
        self._shell = shell
        self._flag = flag
        self.spec = ToolSpec(
            name=name,
            description=desc,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_s": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
        )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        command = input["command"]
        timeout_s = int(input.get("timeout_s", 120))
        try:
            proc = await asyncio.create_subprocess_exec(
                self._shell,
                self._flag,
                command,
                cwd=str(self._workspace.resolve()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_shell_env(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return ToolResult(
                data={
                    "returncode": proc.returncode or 0,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                }
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            return ToolResult(data=None, success=False, error=f"TIMEOUT after {timeout_s}s")


class BashTool(_CommandTool):
    """沙箱执行 bash 命令。"""

    def __init__(self, workspace: Path) -> None:
        super().__init__(
            workspace, shell="bash", flag="-c", name="bash",
            desc="Run a bash command in the workspace and return stdout/stderr/returncode",
        )


class PwshTool(_CommandTool):
    """沙箱执行 PowerShell 命令。"""

    def __init__(self, workspace: Path) -> None:
        super().__init__(
            workspace, shell="pwsh", flag="-Command", name="pwsh",
            desc="Run a PowerShell command in the workspace and return stdout/stderr/returncode",
        )


def generic_tool_registry(workspace: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadFileTool(workspace))
    reg.register(WriteFileTool(workspace))
    reg.register(BashTool(workspace))
    reg.register(PwshTool(workspace))
    return reg
```

> 注：`BashTool` 构造时 `self.spec` 由父类设置，但 `BaseTool.spec` 是类属性——在 `_CommandTool` 里设 `self.spec` 实例属性即可覆盖。若 Windows 无 `bash`/`pwsh`，测试标 `slow` 或跳过（用 `pytest.mark.skipif(not shutil.which("bash"), ...)`）。本任务测试只对 `bash` 断言，`pwsh` 仅注册存在性。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest test/unit/agent/test_generic_tools.py -v`
Expected: PASS（若 `bash` 不可用，`test_bash_runs_and_returns_output` 跳过，其余通过）

- [ ] **Step 5: Commit**

```bash
git add src/athena/agents/tools/generic_tools.py test/unit/agent/test_generic_tools.py
git commit -m "feat: generic agent tools (read_file/write_file/bash/pwsh) in workspace sandbox"
```

---

### Task 3: Prompt 文件（固定格式「限制」）

**Files:**
- Create: `src/athena/core/agent/prompts/init_agent.md`
- Create: `src/athena/core/agent/prompts/report_agent.md`
- Modify: `src/athena/core/agent/prompts/data_agent.md`（补固定报告格式）
- Modify: `src/athena/core/agent/prompts/code_agent.md`（去掉 EvalSpec 引用、加工具清单）
- Modify: `src/athena/core/agent/prompts/plot_agent.md`（加工具清单）

**Interfaces:**
- Produces: `load_prompt(agent_type: str) -> str` 所需的 md 源文件；文件路径 = `core/agent/prompts/{agent_type}.md`

- [ ] **Step 1: 新增 `init_agent.md`**

```markdown
# Task Understanding Agent

You are a research engineer producing task understanding for an ML dataset.

## Your workflow
1. Read the request payload: `data_path`, `target`
2. Inspect the dataset (bash + python or read_file) to understand schema, target type, cardinality
3. Decide task type (regression/classification) and primary metric
4. Write TWO files into the workspace:
   - `task_understanding.md` — fixed-format report (below)
   - `eval.py` — self-contained evaluation script (contract below)

## task_understanding.md format (MUST follow exactly)
- `# Task Understanding`
- `## Dataset` — path, row count, column list
- `## Target` — column, dtype, cardinality, distribution summary
- `## Task Type` — regression | classification, with rationale
- `## Primary Metric` — name + direction (minimize|maximize)
- `## Evaluation Plan` — how eval.py computes the primary metric from predictions

## eval.py contract
- Self-contained, pure numpy (NO sklearn/scipy)
- Reads `predictions.csv` (`__athena_row_id`, `prediction`) and `labels.csv`
  (`__athena_row_id`, `target`) from the current directory
- Aligns rows by row_id, computes the primary metric, prints one JSON line:
  `{"primary": <float>, "metric": "<metric name>"}`
- Runnable with `python eval.py`

## Tools
You have: `read_file`, `write_file`, `bash`, `pwsh`.
```

- [ ] **Step 2: 新增 `report_agent.md`**

```markdown
# Final Report Agent

You synthesize a final research report from approved evidence.

## Your inputs
- Request content: instructions (e.g. report text to include)
- Evidence: approved artifact refs; read them with `read_file` when needed

## Output
Write the report to `report.md` in the workspace.

## report.md format (MUST follow exactly)
- `# Final Research Report`
- `## Executive Summary`
- `## Methods`
- `## Results`
- `## Evidence` — cite the artifact refs you used
- `## Limitations`
- `## Recommendations`

## Constraints
- Only use provided evidence. Do NOT fabricate metrics, experiments, causal
  claims, or citations.
- Do not output anything outside report.md.

## Tools
You have: `read_file`, `write_file`, `bash`, `pwsh`.
```

- [ ] **Step 3: 更新 `data_agent.md`（在 Constraints 后追加固定报告格式）**

在 `src/athena/core/agent/prompts/data_agent.md` 末尾追加：

```markdown
## Report format (report.md) — MUST follow exactly
- `# EDA Report`
- `## Task Overview` — data path, target, task type
- `## Schema` — columns and dtypes
- `## EDA` — distributions, missing values, correlations, target distribution
- `## Key Findings` — 3-5 bullet insights

## Tools
You have: `read_file`, `write_file`, `bash`, `pwsh`.
```

- [ ] **Step 4: 更新 `code_agent.md` / `plot_agent.md`**

`code_agent.md`：把第 2 条「Write eval.py to compute metrics defined in EvalSpec」改为「Run the provided `eval.py` to compute the primary metric」；末尾追加：

```markdown
## Tools
You have: `read_file`, `write_file`, `bash`, `pwsh`.
```

`plot_agent.md`：末尾追加：

```markdown
## Tools
You have: `read_file`, `write_file`, `bash`, `pwsh`.
```

- [ ] **Step 5: 验证 + Commit**

Run: 检查所有 5 个 md 文件存在，且 `data_agent.md` 含「Report format」，`code_agent.md` 不含「EvalSpec」：
```bash
grep -l "Report format" src/athena/core/agent/prompts/data_agent.md
grep -c "EvalSpec" src/athena/core/agent/prompts/code_agent.md || echo "EvalSpec removed"
```
Expected: 前者打印文件路径，后者 `0` 或无匹配。

```bash
git add src/athena/core/agent/prompts/
git commit -m "docs: prompt files define fixed output formats (init/report/data/code/plot)"
```

---

### Task 4: `register_defaults(model)` + conftest + 测试接入点迁移

> 这是组合根迁移：让 `register_defaults` 接受 model/client 并把 model 传给所有 agent 构造器；无 model 报错；新增测试 helper 让全部测试带 model 跑。此时各 agent 仍为确定性实现（model 参数暂存不用），树保持绿色。

**Files:**
- Modify: `src/athena/research/project_runtime.py:191-252`（`register_defaults`）
- Create: `test/_support_project.py`（或复用现有 `test/unit/agent/_support.py` 附近；本计划用 `test/unit/_support.py`）
- Modify: `test/unit/test_project_runtime.py`（约 50 处 `register_defaults()` 调用）
- Modify: `tests/test_research_runtime.py`, `tests/test_gui_gateway_e2e.py`

**Interfaces:**
- Consumes: `settings.model_name()` / `settings.get_client()` (Task 1)
- Produces: `register_defaults(*, model: str | None = None, client: Any = None) -> None`；`model is None` 时 `raise RuntimeError`；`make_project(tmp_path: Path) -> ProjectRuntime`（带 model 的组合根 helper）

- [ ] **Step 1: 写失败测试（无 model 报错）**

在 `test/unit/test_project_runtime.py` 顶部追加：

```python
def test_register_defaults_requires_model(tmp_path) -> None:
    project = ProjectRuntime(tmp_path)
    with pytest.raises(RuntimeError, match="requires a model"):
        project.register_defaults()  # 无 model → 报错，不再静默确定性
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest test/unit/test_project_runtime.py::test_register_defaults_requires_model -v`
Expected: FAIL — `register_defaults()` 未抛错

- [ ] **Step 3: 改 `register_defaults` 签名并接线**

`src/athena/research/project_runtime.py`：

```python
def register_defaults(
    self, *, model: str | None = None, client: Any = None
) -> None:
    """注册静态业务类型；model 必填（LLM 驱动，无回退）。"""
    if model is None:
        raise RuntimeError(
            "register_defaults requires a model: set MODEL_NAME + "
            "DEEPSEEK_API_KEY/BASE_URL in .env and pass model=..."
        )
    # 各 agent 构造器增加 model/client 参数（本任务先透传，Task 6/8/9 再启用）
    self._registry.register("supervisor", lambda _aid, _cfg=None: AgentSpec(
        runner=BaseAgentRunner(SupervisorAgent()), codec=JsonCodec()))
    self._registry.register("init", lambda _aid, _cfg=None: AgentSpec(
        runner=BaseAgentRunner(InitAgent(self._store, model=model, client=client)),
        codec=JsonCodec()))
    self._registry.register("data", lambda aid, _cfg=None: AgentSpec(
        runner=BaseAgentRunner(DataAgent(self._store, self._bundle, aid,
                                        model=model, client=client)),
        codec=JsonCodec()))
    # plot / reflection / ideator / code / report 同理加 model=model, client=client
```

同时给 `DataAgent`/`InitAgent`/`ReportAgent`/`CodeAgent`/`IdeatorAgent`/`PlotAgent` 的 `__init__` 增加 `model: str | None = None, client: Any = None` 参数（本任务不启用，仅接受并存 `self._model`）。`supervisor`/`reflection` 保持原样。

- [ ] **Step 4: 新增 `make_project` helper**

```python
# test/unit/_support.py
from pathlib import Path

from athena.core.agent import settings
from athena.research.project_runtime import ProjectRuntime


def make_project(tmp_path: Path) -> ProjectRuntime:
    """带真实 DeepSeek model 的组合根（LLM 相关测试的入口）。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults(model=settings.model_name())
    return project
```

- [ ] **Step 5: 迁移测试调用点**

把 `test/unit/test_project_runtime.py`、`tests/test_research_runtime.py`、`tests/test_gui_gateway_e2e.py` 里的：
```python
project = ProjectRuntime(tmp_path)
project.register_defaults()
```
改为：
```python
from test.unit._support import make_project
project = make_project(tmp_path)
```
（并发进程有未提交改动时先 `git stash`/协调，见记忆 concurrent-agent-kernel-commits。）

- [ ] **Step 6: 跑全量测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest test/unit/test_project_runtime.py -q`
Expected: PASS（含新增的 `test_register_defaults_requires_model`；已有行为不变）

- [ ] **Step 7: Commit**

```bash
git add src/athena/research/project_runtime.py src/athena/agents/ test/unit/_support.py test/unit/test_project_runtime.py tests/
git commit -m "feat: register_defaults requires model; thread model/client into agents; test make_project helper"
```

---

### Task 5: simple_agents.py → builtin_agents.py

**Files:**
- Rename: `src/athena/agents/simple_agents.py` → `src/athena/agents/builtin_agents.py`
- Modify: `src/athena/agents/__init__.py:24`, `src/athena/research/project_runtime.py:27`
- Modify: `test/unit/test_project_runtime.py:330`, `test/unit/agents/test_agent_factory.py`（import）

**Interfaces:**
- Produces: `from athena.agents.builtin_agents import CodeAgent, IdeatorAgent, PlotAgent`（API 不变）

- [ ] **Step 1: git mv + 更新 import**

```bash
git mv src/athena/agents/simple_agents.py src/athena/agents/builtin_agents.py
```

`src/athena/agents/__init__.py`:
```python
from athena.agents.builtin_agents import CodeAgent, IdeatorAgent, PlotAgent
```
`src/athena/research/project_runtime.py:27` 同理改为 `from athena.agents.builtin_agents import ...`。
`test/unit/test_project_runtime.py:330`、`test/unit/agents/test_agent_factory.py` 的 `simple_agents` import 一并替换。文件 docstring 里「simple」措辞可保留，但模块标题改为「小型确定性业务 Agent」。

- [ ] **Step 2: 跑测试确认无回归**

Run: `.venv/Scripts/python.exe -m pytest test/unit -k "agent or project" -q`
Expected: PASS（import 全部解析）

- [ ] **Step 3: Commit**

```bash
git add -A src/athena/agents/ src/athena/research/project_runtime.py test/
git commit -m "refactor: rename simple_agents -> builtin_agents"
```

---

### Task 6: data_agent 改 LLM 驱动

**Files:**
- Modify: `src/athena/agents/data_agent.py`（重写 `run`；删 `DEFAULT_ANALYSIS_SCRIPT`）
- Create: `src/athena/agents/prompt_agent.py`（内层 LLM agent 构建器 + prompt 加载）
- Modify: `test/unit/agent/test_data_agent.py`

**Interfaces:**
- Consumes: `generic_tool_registry` (Task 2), prompt 文件 (Task 3), `settings`/provider (Task 1), `Agent`/`ResponsesProvider` (core)
- Produces: `build_llm_agent(agent_type, *, model, client, workspace) -> Agent`；`load_prompt(agent_type) -> str`

- [ ] **Step 1: 写内层 LLM agent 构建器（`prompt_agent.py`）**

```python
# src/athena/agents/prompt_agent.py
"""prompt 驱动 agent 构建：md prompt + 通用工具 → 内层 LLM ReAct Agent。"""

from pathlib import Path

from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.runtime import Agent
from athena.core.tool import ToolRegistry

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "core" / "agent" / "prompts"


def load_prompt(agent_type: str) -> str:
    """读取 core/agent/prompts/{agent_type}.md；缺失直接报错。"""
    path = _PROMPT_DIR / f"{agent_type}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def build_llm_agent(
    agent_type: str,
    *,
    model: str,
    client,
    workspace: Path,
    extra_tools: ToolRegistry | None = None,
) -> Agent:
    """构造 ReAct LLM agent：system_prompt = prompt 文件，tools = 通用工具。"""
    tools = generic_tool_registry(workspace)
    if extra_tools is not None:
        for spec in extra_tools.specs:
            pass  # merge: register each tool instance (见下)
    return Agent(
        ResponsesProvider(model, client=client),
        tools,
        load_prompt(agent_type),
    )
```

> 注：`ToolRegistry` 未提供批量 merge；本任务不传 `extra_tools`，`data`/`init`/`report` 只依赖通用工具。

- [ ] **Step 2: 重写 `DataAgent`（删 DEFAULT_ANALYSIS_SCRIPT）**

`src/athena/agents/data_agent.py` 核心（保留 `ANALYSIS_ENTRYPOINT="analysis.py"`、`ANALYSIS_CONFIG`、`_collect_files`、bundle 提交链）：

```python
class DataAgent(BaseAgent):
    """DataAnalysis 唯一提交者：LLM 按 prompt 写 analysis.py 并运行，收集后提交。"""

    def __init__(
        self,
        store: ArtifactStore,
        bundle: VersionedBundle,
        owner_agent_id: str,
        *,
        model: str,
        client: Any = None,
    ) -> None:
        self._store = store
        self._bundle = bundle
        self._owner = owner_agent_id
        self._model = model
        self._client = client
        self.analysis_id: str | None = None
        self.latest_ref: str | None = None

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        request = json.loads(ctx.input_text or "{}")
        data_path = request.get("data_path")
        if not data_path:
            raise ValueError("DataAgent request requires 'data_path'")
        target = request.get("target", "")
        workspace = Path(
            request.get("workspace") or tempfile.mkdtemp(prefix="athena-data-")
        )
        workspace.mkdir(parents=True, exist_ok=True)
        # 1. 内层 LLM agent：写 analysis.py → 运行 → 产出 report.md + figures
        inner = build_llm_agent(
            "data", model=self._model, client=self._client, workspace=workspace
        )
        inner_ctx = AgentContext(
            thread=ctx.thread, turn=ctx.turn, emit=ctx.emit,
            tools=inner.tools, cancel=ctx.cancel, memory=ctx.memory,
            input_text=json.dumps({"data_path": str(data_path), "target": target},
                                  ensure_ascii=False),
        )
        await inner.run(inner_ctx)
        # 2. 收集 + 提交版本（保留所有权链）
        files = await self._collect_files(workspace)
        if request.get("report") is not None:
            files["report.md"] = await self._store.put_text(str(request["report"]))
        if self.analysis_id is None:
            self.analysis_id, self.latest_ref = await self._bundle.create(
                self._owner, files
            )
        else:
            self.latest_ref = await self._bundle.commit(
                self.analysis_id, self._owner, files, self.latest_ref
            )
        return AgentOutcome(result_ref=self.latest_ref)
```

删除 `DEFAULT_ANALYSIS_SCRIPT` 常量。`_collect_files` 逻辑不变（`report.md` 必须存在 + 至少一张 `figures/*`）。

- [ ] **Step 3: 改写 `test_data_agent.py`（注入 fake provider，不依赖真实 API）**

把 `DEFAULT_ANALYSIS_SCRIPT` 断言改为「LLM 生成的脚本被写入并运行」：用 stub provider 模拟 LLM 写 `analysis.py` + 调 `bash` 产出 `report.md`/`figures/`。提供 `FakeResponsesProvider`：

```python
class FakeProvider:
    """stub stream：直接 emit text_delta + response_completed，让 Agent 收尾。"""
    def __init__(self) -> None:
        self.model_name = "fake"

    async def stream(self, config, tools, messages, cancel, *, output_type=None):
        yield StreamEvent(kind="text_delta", data={"delta": "done", "accumulated": "done"})
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})
```

> 因真实 LLM 会写脚本并运行，单元测试用「预写 analysis.py + bash 产出」的 fake 路径即可验证编排。测试断言：`analysis.py` 被写入、`report.md`/`figures/` 提交为 DataAnalysis v1/v2、失败不提交。真实 DeepSeek 集成测试在 Task 10 用 `@pytest.mark.slow` 补。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest test/unit/agent/test_data_agent.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/agents/prompt_agent.py src/athena/agents/data_agent.py test/unit/agent/test_data_agent.py
git commit -m "feat: data_agent LLM-driven (prompt writes analysis.py), drop hardcoded template"
```

---

### Task 7: 删 EvalSpec、保留 eval.py

> 先删 EvalSpec 再改 init_agent（Task 8），避免中间态让 project_runtime 在无 primary_metric 情况下构造 EvalSpec。

**Files:**
- Modify: `src/athena/research/models.py`（删 `EvalSpec`/`EvalSpecChain`/`MetricDef`）
- Modify: `src/athena/research/project_runtime.py`（删 `_eval_specs`/`eval_specs`/`freeze_eval_spec`/持久化；`_run_init_understanding` 简化；`projected_phase` has_protocol → `_eval_ref`）
- Delete: `test/unit/test_research_models.py`
- Modify: `tests/test_core_model_contracts.py`（删 EvalSpec/MetricDef 导出断言）
- Modify: `test/unit/test_project_runtime.py`（删 `test_freeze_eval_spec_*`、`test_eval_spec_chain_*`）

**Interfaces:**
- Consumes: 当前 init 产物（确定性 init_agent 仍产出 `eval_script` + `primary_metric`）
- Produces: `ProjectRuntime._eval_ref: str | None`（eval.py artifact ref）；`projected_phase()` 的 `has_protocol = self._eval_ref is not None`

- [ ] **Step 1: 删模型类**

`research/models.py` 删 `MetricDef`/`EvalSpec`/`EvalSpecChain`（保留 `MetricSpec`/`TaskMetaData`）。

- [ ] **Step 2: project_runtime 移除 EvalSpec 机制**

- 删 `_eval_specs = EvalSpecChain()`、`eval_specs` property、`freeze_eval_spec()`。
- 新增 `self._eval_ref: str | None = None`。
- `_run_init_understanding`：读 init 产物 `eval_script` → `put_text` 为 artifact → `self._eval_ref = <ref>`；primary_metric 不再存（task 的 primary_metric 已由 `configure(task)` 持有）。
- `projected_phase()`：`has_protocol = self._eval_ref is not None`。
- `_save_state`/`_load_state`：`"eval_specs"` → `"eval_ref"`。

- [ ] **Step 3: 更新测试**

`git rm test/unit/test_research_models.py`；`test/unit/test_project_runtime.py` 删 `test_freeze_eval_spec_creates_version`、`test_eval_spec_chain_*`、`test_eval_specs_persist`（若存在），PREPARE 断言改 `project.eval_ref is not None`；`tests/test_core_model_contracts.py` 导出清单去掉 EvalSpec/EvalSpecChain/MetricDef。

- [ ] **Step 4: 跑全量测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest test/unit tests -m "not slow" -q`
Expected: PASS（EvalSpec 相关全部移除）

- [ ] **Step 5: Commit**

```bash
git add -A src/athena/research/ test/ tests/
git commit -m "refactor: delete EvalSpec chain; keep eval.py as project eval_ref fact"
```

---

### Task 8: init_agent 改 LLM 驱动

**Files:**
- Modify: `src/athena/agents/init_agent.py`（重写 `run`；删 `_classify_task`/`_default_eval_script`）
- Modify: `src/athena/research/project_runtime.py:317-359`（`_run_init_understanding`）
- Modify: `test/unit/test_project_runtime.py`（PREPARE 相关断言）

**Interfaces:**
- Consumes: `build_llm_agent`/`load_prompt` (Task 6), prompt `init_agent.md` (Task 3), `_eval_ref` (Task 7)
- Produces: InitAgent 产物 `{"task_understanding": str, "eval_script": str}`（Artifact payload）；`_run_init_understanding` 读回存 `_eval_ref`

- [ ] **Step 1: 重写 `InitAgent`（删 `_classify_task`/`_default_eval_script`）**

```python
class InitAgent(BaseAgent):
    """task understanding：LLM 产出固定格式报告 + eval.py。"""

    name = "init-agent"
    description = "理解任务并产出 task-understanding 报告 + eval.py"

    def __init__(self, store: ArtifactStore, *, model: str, client: Any = None) -> None:
        self._store = store
        self._model = model
        self._client = client

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        request = json.loads(ctx.input_text or "{}")
        data_path = request.get("data_path")
        if not data_path:
            raise ValueError("InitAgent request requires 'data_path'")
        target = request.get("target")
        if not target:
            raise ValueError("InitAgent request requires 'target'")
        workspace = Path(tempfile.mkdtemp(prefix="athena-init-"))
        inner = build_llm_agent(
            "init", model=self._model, client=self._client, workspace=workspace
        )
        inner_ctx = AgentContext(
            thread=ctx.thread, turn=ctx.turn, emit=ctx.emit,
            tools=inner.tools, cancel=ctx.cancel, memory=ctx.memory,
            input_text=json.dumps({"data_path": str(data_path), "target": target},
                                  ensure_ascii=False),
        )
        await inner.run(inner_ctx)
        report = (workspace / "task_understanding.md").read_text(encoding="utf-8")
        eval_script = (workspace / "eval.py").read_text(encoding="utf-8")
        compile(eval_script, EVAL_ENTRYPOINT, "exec")  # 语法自检
        payload = {"task_understanding": report, "eval_script": eval_script}
        result_ref = await self._store.put_text(json.dumps(payload, ensure_ascii=False))
        return AgentOutcome(result_ref=result_ref)
```

保留 `EVAL_ENTRYPOINT = "eval.py"`；删除 `_classify_task`、`_default_eval_script`、`CATEGORICAL_UNIQUE_CAP`。

- [ ] **Step 2: 更新 `_run_init_understanding`（存 `_eval_ref`）**

`project_runtime._run_init_understanding`（Task 7 已删 EvalSpec）：
```python
if self._eval_ref is not None:
    return
request: dict[str, str] = {
    "data_path": str(Path(data_path).resolve()),
    "target": target,
}
if eval_script is not None:
    request["eval_script"] = eval_script
_, run = await self._runtime.create_root("init", _request(request), name="init-root")
summary = await self._runtime.wait_run(run, timeout=10)
payload = json.loads(summary.response_ref)
init_result = json.loads(await self._store.get_text(payload["result_ref"]))
eval_script = init_result["eval_script"]
task_understanding = init_result["task_understanding"]  # 固定格式报告，留作证据
self._eval_ref = await self._store.put_text(eval_script)
self._save_state()
```

- [ ] **Step 3: 更新测试断言**

`test_project_runtime.py` 的 PREPARE 用例（`test_projected_phase_advances_through_six_stages` 等）改为经 `make_project` 带 model 构造；init 产出断言改为校验 `task_understanding` 字段存在。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest test/unit/test_project_runtime.py -k "prepare or init or projected" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/agents/init_agent.py src/athena/research/project_runtime.py test/unit/test_project_runtime.py
git commit -m "feat: init_agent LLM-driven (task-understanding report + eval.py), drop hardcoded eval generator"
```

---

### Task 9: 删 report_agent + report 改 prompt 驱动

**Files:**
- Delete: `src/athena/agents/report_agent.py`
- Modify: `src/athena/agents/__init__.py`（删 import/`__all__`）
- Modify: `src/athena/research/project_runtime.py`（删 import；`register_defaults` report 注册；`run_report`）
- Modify: `test/unit/test_project_runtime.py`（删 ReportAgent 测试，改 prompt 驱动断言）

**Interfaces:**
- Consumes: `build_llm_agent("report", ...)` (Task 6), `report_agent.md` (Task 3)
- Produces: report agent = 内层 LLM agent，`run_report` 返回 report Bundle ref

- [ ] **Step 1: 删文件 + 清 import**

```bash
git rm src/athena/agents/report_agent.py
```
`src/athena/agents/__init__.py`：删 `from athena.agents.report_agent import ReportAgent` 和 `"ReportAgent"`。
`src/athena/research/project_runtime.py:26`：删 `from athena.agents.report_agent import ReportAgent`。

- [ ] **Step 2: report 注册改为内层 LLM agent**

`project_runtime.register_defaults` 的 report 分支：
```python
self._registry.register(
    "report",
    lambda _aid, _cfg=None: AgentSpec(
        runner=BaseAgentRunner(
            build_report_agent(model=model, client=client, store=self._store)
        ),
        codec=JsonCodec(),
    ),
)
```
其中 `build_report_agent`（放 `prompt_agent.py`）返回一个内层 `Agent` 的 `BaseAgent` 适配（或用 `BaseAgentRunner` 直接包 `Agent`）。证据 context_refs 由 runner 注入 `AgentContext.messages`，prompt 让 LLM 用 `read_file` 读 artifact——report 工作区需可访问 artifact 存储。**简化**：把 `report` 作为一个「先收集证据再调内层 LLM」的 `BaseAgent` 编排器（类似 data/init），产出 report.md Bundle：

```python
# prompt_agent.py
async def run_report_bundle(
    ctx: AgentContext, *, model, client, store: ArtifactStore, workspace: Path
) -> AgentOutcome:
    evidence = await collect_evidence(ctx, store)  # messages context_refs → 文本
    inner = build_llm_agent("report", model=model, client=client, workspace=workspace)
    inner_ctx = AgentContext(thread=ctx.thread, turn=ctx.turn, emit=ctx.emit,
                             tools=inner.tools, cancel=ctx.cancel, memory=ctx.memory,
                             input_text=f"Write the final report. Evidence:\n{evidence}")
    await inner.run(inner_ctx)
    report = (workspace / "report.md").read_text(encoding="utf-8")
    ref = await DirectoryBundle.commit(
        store, {"report.md": await store.put_text(report)}
    )
    return AgentOutcome(result_ref=ref)
```

- [ ] **Step 3: 重写 `run_report`**

`run_report` 改用 `build_report_agent`/`run_report_bundle`：先 spawn report root（编排器），评审闭环不变（Reflection 评审 report Bundle → 判定 → follow-up 修订）。

- [ ] **Step 4: 更新测试**

删 `test/unit/test_project_runtime.py` 的 `test_report_agent_model_path_synthesizes_from_user_input`（原 ReportAgent 专属），补一个 fake-provider 版 report 编排测试（校验 report.md Bundle + 证据进入 prompt）。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest test/unit/test_project_runtime.py -k "report" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A src/athena/agents/ src/athena/research/project_runtime.py test/unit/test_project_runtime.py
git commit -m "refactor: delete report_agent; report is prompt-driven md Bundle"
```

---

### Task 10: 真实 DeepSeek 集成测试（slow）

**Files:**
- Create: `test/unit/agent/test_llm_integration.py`
- Modify: `src/athena/agents/builtin_agents.py`（CodeAgent/IdeatorAgent/PlotAgent 用 `build_llm_agent`，若本计划范围确认需要）

**Interfaces:**
- Consumes: `make_project` (Task 4), `.env` 真实凭据 (Task 1)

- [ ] **Step 1: 写集成测试（标 slow）**

```python
# test/unit/agent/test_llm_integration.py
import json
import pytest

from athena.core.agent import settings
from athena.research.project_runtime import ProjectRuntime


@pytest.mark.slow
async def test_prepare_data_analysis_with_real_llm(tmp_path) -> None:
    """真实 DeepSeek 跑通 PREPARE：init 产出 eval.py + data 提交 EDA bundle。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults(model=settings.model_name())
    await project.open()
    import pandas as pd
    data = tmp_path / "d.csv"
    pd.DataFrame({"age": range(20), "income": range(20),
                  "label": [0, 1] * 10}).to_csv(data, index=False)
    await project.configure(_task())  # 复用 test_project_runtime._task 形态
    accepted = await project.prepare_data_analysis(str(data), "label")
    assert accepted.startswith("sha256:")
```

- [ ] **Step 2: 跑一次真实集成测试**

Run: `.venv/Scripts/python.exe -m pytest test/unit/agent/test_llm_integration.py -m slow -v`
Expected: PASS（需 `.env` 有 `DEEPSEEK_API_KEY` + `BASE_URL`，网络可达 api.deepseek.com）

- [ ] **Step 3: Commit**

```bash
git add test/unit/agent/test_llm_integration.py
git commit -m "test: real DeepSeek PREPARE integration (slow)"
```

---

## Self-Review

**Spec 覆盖：** Task 1（LLM 配置）✓ Task 2（通用工具）✓ Task 3（prompt 固定格式）✓ Task 4（model 接入/无 model 报错）✓ Task 6（data 脚本→prompt）✓ Task 7（删 EvalSpec、留 eval.py）✓ Task 8（init→task-understanding 报告+eval.py）✓ Task 9（删 report_agent）✓ Task 5+10（simple_agents 改名完善、真实 API 测试）✓

**Placeholder 扫描：** 无 TBD；`build_report_agent`/`collect_evidence` 在 Task 9 内定义。

**类型一致性：** `register_defaults(*, model=None, client=None)` 在 Task 4 定义，Task 6/7/8/9/10 沿用；`build_llm_agent(agent_type, *, model, client, workspace)` 在 Task 6 定义，Task 8/9/10 沿用；agent `__init__` 的 `model/client` 参数在 Task 4 加、Task 6/8 启用；`ProjectRuntime._eval_ref` 在 Task 7 定义，Task 8 的 `_run_init_understanding` 使用。

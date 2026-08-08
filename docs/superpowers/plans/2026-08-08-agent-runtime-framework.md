# Agent 运行时框架实现计划（Codex-CLI 风格）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 AgentKernel 引入 LLM 驱动的 `Agent`（Codex-CLI 式工具循环）作通用执行引擎，支持可选结构化输出 `output_type`。

**Architecture:** `Agent`（`core/agent/runtime.py`）加可选 `output_type`（结构化终止：JSON 校验失败回喂≤3，经注入的 ArtifactStore 落盘）；`ResponsesProvider.stream` 支持 `response_format`（JSON-schema）；新增 `agents/agent_factory.py` 与 `agents/tools/`（write_script/run_script/commit_result）；`ProjectRuntime.register_defaults(model=None)` 预留 LLM 映射（首版为空）。复用 `BaseAgentRunner`，`data`/`code` 不变，supervisor 保留 Manager。

**Tech Stack:** Python 3.11+，pydantic v2，openai，pytest。

## Global Constraints

- `data`（DataAgent）与 `code`（CodeAgent）**不**改为 LLM Agent。
- supervisor 的编排工具 `spawn`/`send`/`wait_for`（Manager 模式）保留不动。
- `output_type` 可选：传入才走 `response_format`；不传保持自由文本（现状）。
- `create_code_agent` 参数顺序保持 `(model, tools, system_prompt, config)`；`AgentConfig` 字段不变。
- 未配置 model 时 `register_defaults` 回退现有确定性骨架。

---

### Task 1: Provider `stream` 支持 `response_format`

**Files:**
- Modify: `src/athena/core/agent/provider.py`（`stream` 方法）
- Test: `test/unit/test_agent.py`（新增 `TestProviderResponseFormat`）

**Interfaces:**
- Produces: `ResponsesProvider.stream(config, tools, messages, cancel, *, output_type: type[BaseModel] | None = None)` — `output_type` 非空时，`kw["response_format"]={"type":"json_schema","json_schema":{"name":t.__name__,"schema":t.model_json_schema()}}`。

- [ ] **Step 1: 写失败测试**（追加到 `test/unit/test_agent.py` 末尾）

```python
class _CaptureClient:
    def __init__(self):
        self.kwargs: dict = {}

    @property
    def chat(self):
        class _Completions:
            def __init__(self, owner):
                self._owner = owner

            async def create(self, **kw):
                self._owner.kwargs = kw
                chunks = [type("C", (), {"choices": []})()]

                async def gen():
                    for c in chunks:
                        yield c

                return gen()

        class _Chat:
            @property
            def completions(self):
                return _Completions(self._owner)

        return _Chat()
```

```python
class _Out(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_stream_sets_response_format_when_output_type_given() -> None:
    client = _CaptureClient()
    provider = ResponsesProvider("model", client=client)
    events = [
        e
        async for e in provider.stream(
            AgentConfig(), ToolRegistry(), [], asyncio.Event(), output_type=_Out
        )
    ]
    rf = client.kwargs["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "_Out"
    assert rf["json_schema"]["schema"] == _Out.model_json_schema()
    assert any(e.kind == "response_completed" for e in events)


@pytest.mark.asyncio
async def test_stream_omits_response_format_without_output_type() -> None:
    client = _CaptureClient()
    provider = ResponsesProvider("model", client=client)
    events = [
        e
        async for e in provider.stream(AgentConfig(), ToolRegistry(), [], asyncio.Event())
    ]
    assert "response_format" not in client.kwargs
    assert any(e.kind == "response_completed" for e in events)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest test/unit/test_agent.py::test_stream_sets_response_format_when_output_type_given -q`
Expected: FAIL — `TypeError: stream() got an unexpected keyword argument 'output_type'`

- [ ] **Step 3: 实现**（`src/athena/core/agent/provider.py`）

在 `stream` 签名与 `kw` 构造处改动：

```python
    async def stream(
        self,
        config: "AgentConfig",
        tools: "ToolRegistry",
        messages: list[ModelMessage],
        cancel: asyncio.Event,
        *,
        output_type: type | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        api_msgs = _to_api(messages)
        tool_defs = [spec.to_openai_tool() for spec in tools.specs]

        kw: dict = dict(
            model=self.model_name,
            messages=api_msgs,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            stream=True,
        )
        if output_type is not None:
            kw["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_type.__name__,
                    "schema": output_type.model_json_schema(),
                },
            }
        if tool_defs:
            kw["tools"] = tool_defs
            kw["tool_choice"] = "auto"
```

在文件顶部 import 区无需改动（`type` 内建；`model_json_schema` 由 pydantic 提供）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest test/unit/test_agent.py::TestProviderResponseFormat -q`
Expected: PASS（2 个测试）

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent/provider.py test/unit/test_agent.py
git commit -m "feat: provider stream supports response_format for structured output"
```

---

### Task 2: `Agent` 可选 `output_type` 结构化终止

**Files:**
- Modify: `src/athena/core/agent/runtime.py`（`Agent.__init__`、`Agent.run`、`_sampling_loop` 的 stream 调用）
- Modify: `test/unit/test_agent.py`（既有 fake provider 加 `**_kwargs`；`vars(agent)` 与 `create_code_agent` 参数断言更新）

**Interfaces:**
- Produces: `Agent(model, tools, system_prompt, config, *, output_type: type[BaseModel] | None = None, artifacts: ArtifactStore | None = None)`；`run()` 在 `output_type` 有值时：`output_type.model_validate_json(outcome.text)`，校验失败回喂重试 ≤3，成功 `instance.model_dump_json()` 写入 `artifacts`（无 artifacts 回退 `result://{turn_id}`）。

- [ ] **Step 1: 更新既有断言（先让它继续绿）**

`test/unit/test_agent.py` 中：
- `test_public_agent_exports_point_to_canonical_owners` 不改。
- `test_code_agent_interface_has_four_parameters_and_four_fields` 保持（`create_code_agent` 参数不变）。
- `test_environment_builds_three_independent_core_agents`：把 `set(vars(agent)) == {"model","tools","system_prompt","config"}` 改为 `{"model","tools","system_prompt","config","_output_type","_artifacts"}`。
- 所有 fake provider 的 `async def stream(self, *_args)` 改为 `async def stream(self, *_args, **_kwargs)`（`test_empty_system_prompt_is_not_added_to_memory` 等 4 处）。

- [ ] **Step 2: 写失败测试**（追加到 `test/unit/test_agent.py`）

```python
class _StructuredOut(BaseModel):
    answer: str


class _StructuredProvider:
    def __init__(self):
        self.calls = 0

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent("text_delta", {"delta": "not json", "accumulated": "not json"})
        else:
            yield StreamEvent("text_delta", {"delta": '{"answer":"hi"}', "accumulated": '{"answer":"hi"}'})
        yield StreamEvent("response_completed")


@pytest.mark.asyncio
async def test_agent_structured_output_validates_retries_and_persists(tmp_path) -> None:
    from athena.storage.artifact_store import LocalArtifactStore
    from athena.core.agent.models import AgentContext

    store = LocalArtifactStore(tmp_path / "artifacts")
    tools = ToolRegistry()
    agent = Agent(
        ResponsesProvider("model"), tools, "system", output_type=_StructuredOut, artifacts=store
    )
    agent.model = _StructuredProvider()
    ctx = AgentContext(
        AthenaThread("t1", "s1", "running", "ctx://0"),
        AthenaTurn("t1.1", "t1", "request", "running"),
        lambda *_a: asyncio.sleep(0),
        tools,
        asyncio.Event(),
    )

    outcome = await agent.run(ctx)

    assert outcome.result_ref.startswith("sha256:")
    assert (await store.get_text(outcome.result_ref)) == '{"answer":"hi"}'
```

- [ ] **Step 3: 运行确认失败**

Run: `pytest test/unit/test_agent.py::test_agent_structured_output_validates_retries_and_persists -q`
Expected: FAIL — `TypeError: Agent.__init__() got an unexpected keyword argument 'output_type'`

- [ ] **Step 4: 实现**（`src/athena/core/agent/runtime.py`）

```python
from pydantic import BaseModel, ValidationError

_MAX_STRUCTURED_RETRIES = 3
```

```python
    def __init__(
        self,
        model: ResponsesProvider,
        tools: ToolRegistry,
        system_prompt: str,
        config: AgentConfig | None = None,
        *,
        output_type: type[BaseModel] | None = None,
        artifacts: "ArtifactStore | None" = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt
        self.config = config or AgentConfig()
        self._output_type = output_type
        self._artifacts = artifacts
```

`run()` 内 `for _ in range(...)` 循环里，把 done 分支改为：

```python
        retries = 0
        for _ in range(self.config.max_turns):
            if ctx.cancel.is_set():
                break
            outcome = await _sampling_loop(self, ctx)
            if outcome.kind == "done":
                if self._output_type is not None:
                    try:
                        instance = self._output_type.model_validate_json(outcome.text)
                    except ValidationError as exc:
                        if retries >= _MAX_STRUCTURED_RETRIES:
                            raise RuntimeError(
                                f"structured output invalid after retries: {exc}"
                            ) from exc
                        retries += 1
                        mem.append(
                            ModelRequest(
                                parts=[
                                    UserPromptPart(
                                        content=(
                                            "Previous JSON output was invalid: "
                                            f"{exc}\nReturn JSON matching the schema."
                                        )
                                    )
                                ]
                            )
                        )
                        continue
                    json_text = instance.model_dump_json()
                    ref = (
                        await self._artifacts.put_text(json_text)
                        if self._artifacts is not None
                        else f"result://{ctx.turn.turn_id}"
                    )
                    return AgentOutcome(
                        result_ref=ref,
                        next_context_ref=f"context://{ctx.turn.turn_id}/next",
                    )
                ref = f"result://{ctx.turn.turn_id}"
                return AgentOutcome(
                    result_ref=ref,
                    next_context_ref=f"context://{ctx.turn.turn_id}/next",
                )
            if outcome.kind == "error":
                raise RuntimeError(outcome.text or "provider stream failed")
```

`_sampling_loop` 里把 provider 调用改为透传 output_type：

```python
            async with aclosing(
                agent.model.stream(
                    agent.config,
                    agent.tools,
                    mem.items,
                    ctx.cancel,
                    output_type=getattr(agent, "_output_type", None),
                )
            ) as stream:
```

- [ ] **Step 5: 运行全部 agent 测试**

Run: `pytest test/unit/test_agent.py -q`
Expected: PASS（新增结构化测试 + 既有测试因 Step 1 更新而绿）

- [ ] **Step 6: 提交**

```bash
git add src/athena/core/agent/runtime.py test/unit/test_agent.py
git commit -m "feat: Agent optional output_type structured termination with retry+persist"
```

---

### Task 3: `agents/agent_factory.py`

**Files:**
- Create: `src/athena/agents/agent_factory.py`
- Create: `test/unit/agents/test_agent_factory.py`（目录 `test/unit/agents/` 不存在则创建 `__init__.py` 不需要）

**Interfaces:**
- Produces: `create_agent_for(agent_type, *, model, tools, system_prompt, output_type=None, artifacts=None, client=None) -> Agent`；`build_orchestration_tools(kernel, agent_id, allowed) -> ToolRegistry`（复用 `RunToolProjector`）。

- [ ] **Step 1: 写失败测试**

```python
from athena.agents.agent_factory import create_agent_for
from athena.core.agent.runtime import Agent
from athena.core.tool import ToolRegistry
from athena.core.agent.provider import ResponsesProvider


class _Result(BaseModel):
    ok: bool


def test_create_agent_for_builds_agent_with_output_type() -> None:
    tools = ToolRegistry()
    agent = create_agent_for(
        "report",
        model="test-model",
        tools=tools,
        system_prompt="prompt",
        output_type=_Result,
    )
    assert isinstance(agent, Agent)
    assert agent._output_type is _Result
    assert agent.model.model_name == "test-model"
    assert agent.tools is tools
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest test/unit/agents/test_agent_factory.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'athena.agents.agent_factory'`

- [ ] **Step 3: 实现**

```python
"""按 agent_type 构造 LLM 驱动的 Agent（Codex-CLI 风格框架）。"""

from typing import TYPE_CHECKING

from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.runtime import Agent

if TYPE_CHECKING:
    from athena.core.agent.models import AgentConfig
    from athena.core.tool import ToolRegistry
    from athena.storage.artifact_store import ArtifactStore


def create_agent_for(
    agent_type: str,
    *,
    model: str,
    tools: "ToolRegistry",
    system_prompt: str,
    output_type: type | None = None,
    artifacts: "ArtifactStore | None" = None,
    client=None,
    name: str | None = None,
) -> Agent:
    """构造一个 LLM 驱动的 Agent 实例。

    ``agent_type`` 用于命名与后续映射扩展；``output_type`` 非空时启用结构化输出。
    """
    provider = ResponsesProvider(model, client=client)
    return Agent(
        provider,
        tools,
        system_prompt,
        output_type=output_type,
        artifacts=artifacts,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest test/unit/agents/test_agent_factory.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/athena/agents/agent_factory.py test/unit/agents/test_agent_factory.py
git commit -m "feat: agent_factory builds LLM-driven Agent per agent_type"
```

---

### Task 4: 业务工具 `write_script` / `run_script` / `commit_result`

**Files:**
- Create: `src/athena/agents/tools/__init__.py`
- Create: `src/athena/agents/tools/script_tools.py`
- Test: `test/unit/agents/test_script_tools.py`

**Interfaces:**
- Produces: `WriteScriptTool(workspace: Path)` — `spec.name="write_script"`，输入 `{path, content}`，写 `workspace/path`；`RunScriptTool(workspace: Path, runtime=None)` — `spec.name="run_script"`，输入 `{path, timeout_s?}`，复用 `LocalExperimentRuntime` 执行并返回 `{returncode, stdout, stderr, files}`；`CommitResultTool(store: ArtifactStore)` — `spec.name="commit_result"`，输入 `{text}`，`store.put_text` 返回 `{ref}`。

- [ ] **Step 1: 写失败测试**

```python
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents.tools.script_tools import (
    CommitResultTool,
    RunScriptTool,
    WriteScriptTool,
)
from athena.core.tool_types import ToolContext
from athena.storage.artifact_store import LocalArtifactStore


def _tctx() -> ToolContext:
    return ToolContext("t", "call-1", lambda *_a: None, None)


def test_write_script_creates_file(tmp_path) -> None:
    tool = WriteScriptTool(tmp_path)
    result = asyncio.run(tool.execute({"path": "a.py", "content": "print(1)"}, _tctx()))
    assert (tmp_path / "a.py").read_text() == "print(1)"


def test_commit_result_persists_to_store(tmp_path) -> None:
    import asyncio
    store = LocalArtifactStore(tmp_path / "a")
    tool = CommitResultTool(store)
    result = asyncio.run(tool.execute({"text": "hello"}, _tctx()))
    assert result.data["ref"].startswith("sha256:")
    assert asyncio.run(store.get_text(result.data["ref"])) == "hello"


def test_run_script_runs_in_workspace(tmp_path) -> None:
    import asyncio
    (tmp_path / "a.py").write_text("print('ok')")
    tool = RunScriptTool(tmp_path)
    result = asyncio.run(tool.execute({"path": "a.py"}, _tctx()))
    assert result.data["returncode"] == 0
    assert "ok" in result.data["stdout"]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest test/unit/agents/test_script_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'athena.agents.tools'`

- [ ] **Step 3: 实现**

`src/athena/agents/tools/__init__.py`：

```python
"""Agent 业务工具集（Codex-CLI 式能力：写脚本 / 沙箱跑脚本 / 提交结果）。"""

from athena.agents.tools.script_tools import CommitResultTool, RunScriptTool, WriteScriptTool

__all__ = ["CommitResultTool", "RunScriptTool", "WriteScriptTool"]
```

`src/athena/agents/tools/script_tools.py`：

```python
"""Agent 脚本执行与结果提交工具。"""

from pathlib import Path

from athena.code.execution import ExecutionRequest, LocalExperimentRuntime
from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.storage.artifact_store import ArtifactStore


class WriteScriptTool(BaseTool):
    """把脚本内容写入工作区。"""

    spec = ToolSpec(
        name="write_script",
        description="Write a script file into the workspace",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        path = (self._workspace / input["path"]).resolve()
        if not path.is_relative_to(self._workspace.resolve()):
            return ToolResult(data=None, success=False, error="path escapes workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(input["content"], encoding="utf-8")
        return ToolResult(data={"path": str(path)})


class RunScriptTool(BaseTool):
    """在工作区沙箱执行脚本并返回输出。"""

    spec = ToolSpec(
        name="run_script",
        description="Run a script in the workspace sandbox and return output",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 120},
            },
            "required": ["path"],
        },
    )

    def __init__(self, workspace: Path, runtime=None) -> None:
        self._workspace = Path(workspace)
        self._runtime = runtime or LocalExperimentRuntime()

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        output = await self._runtime.run(
            ExecutionRequest(
                entrypoint=input["path"],
                cwd=self._workspace,
                timeout_s=int(input.get("timeout_s", 120)),
            )
        )
        return ToolResult(
            data={
                "returncode": output.returncode,
                "stdout": output.stdout,
                "stderr": output.stderr,
                "files": output.files,
            }
        )


class CommitResultTool(BaseTool):
    """把一段结果文本提交为 artifact，返回其引用。"""

    spec = ToolSpec(
        name="commit_result",
        description="Commit result text to the artifact store and return its reference",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ref = await self._store.put_text(input["text"])
        return ToolResult(data={"ref": ref})
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest test/unit/agents/test_script_tools.py -q`
Expected: PASS（3 个测试）

- [ ] **Step 5: 提交**

```bash
git add src/athena/agents/tools/ test/unit/agents/test_script_tools.py
git commit -m "feat: business tools write_script/run_script/commit_result"
```

---

### Task 5: `register_defaults(model=None)` 预留 + kernel 集成

**Files:**
- Modify: `src/athena/research/project_runtime.py`（`register_defaults` 加 `model`/`client` 参数 + 空 `LLM_AGENT_MAPPING`）
- Test: `test/unit/test_project_runtime.py`（新增 kernel 集成测试）

**Interfaces:**
- Produces: `ProjectRuntime.register_defaults(*, model: str | None = None, client=None)`；模块级 `LLM_AGENT_MAPPING: dict[str, dict] = {}`（首版空——按 spec「首版否」，业务 agent 暂不转换）。有 model 且映射非空时才注册 LLM Agent；否则维持确定性骨架。

- [ ] **Step 1: 写失败测试**（追加 `test/unit/test_project_runtime.py`）

```python
@pytest.mark.asyncio
async def test_llm_agent_runs_through_kernel_via_base_agent_runner(tmp_path) -> None:
    """框架集成：Agent(output_type) 经 BaseAgentRunner 在 kernel 跑通一个 turn。"""
    from athena.agents.base_runner import BaseAgentRunner
    from athena.core.agent.provider import ResponsesProvider, StreamEvent
    from athena.core.agent.runtime import Agent
    from athena.core.agent_kernel.codec import JsonCodec
    from athena.core.agent_kernel.types import AgentSpec, RunStatus
    from athena.core.tool import ToolRegistry
    from pydantic import BaseModel

    class Out(BaseModel):
        ok: bool

    class FakeModel:
        async def stream(self, *_args, **_kwargs):
            yield StreamEvent("text_delta", {"delta": '{"ok":true}', "accumulated": '{"ok":true}'})
            yield StreamEvent("response_completed")

    store = project.store
    agent = Agent(ResponsesProvider("m"), ToolRegistry(), "p", output_type=Out, artifacts=store)
    agent.model = FakeModel()
    project.kernel._type_registry.register(
        "llm-demo",
        lambda _aid, _cfg=None: AgentSpec(runner=BaseAgentRunner(agent), codec=JsonCodec()),
    )
    _, run_id = await project.kernel.create_root("llm-demo", {"content": "go"}, name="llm-root")
    summary = await project.kernel.wait_run(run_id, timeout=2)
    assert summary.status.value == "completed"
    response = json.loads(summary.response_ref)
    assert response["result_ref"].startswith("sha256:")
```

- [ ] **Step 2: 运行确认通过（此测试验证框架接线，Task 2 之后即应通过）**

Run: `pytest test/unit/test_project_runtime.py::test_llm_agent_runs_through_kernel_via_base_agent_runner -q`
Expected: PASS — `Agent`（Task 2 后已支持 `output_type`）经 `BaseAgentRunner` 在 kernel 跑通，`result_ref` 为持久化 JSON。

- [ ] **Step 3: 实现 `register_defaults` 预留参数**

`src/athena/research/project_runtime.py` 模块级加：

```python
# LLM Agent 映射（首版为空——spec「首版否」；后续按 spec 映射表填充即可启用转换）
LLM_AGENT_MAPPING: dict[str, dict] = {}
```

`register_defaults` 签名改为：

```python
    def register_defaults(
        self, *, model: str | None = None, client=None
    ) -> None:
```

并在方法开头加文档注释（不改 7 个类型注册逻辑；映射为空时 `model` 不影响行为，供后续启用）：

```python
        # 首版框架：LLM_AGENT_MAPPING 为空，全部保持确定性骨架。
        # 后续填充映射 + 传入 model 时，改用 create_agent_for 注册 LLM Agent。
```

- [ ] **Step 4: 运行全部相关测试**

Run: `pytest test/unit/test_agent.py test/unit/agents/ test/unit/test_project_runtime.py test/unit/agent_kernel/test_data_agent.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/athena/research/project_runtime.py test/unit/test_project_runtime.py
git commit -m "feat: register_defaults(model=None) hook + kernel integration test"
```

---

## Self-Review

- **Spec 覆盖**：`provider.py` response_format → Task 1；`Agent` output_type/回喂/落盘 → Task 2；`agent_factory` → Task 3；`agents/tools/` → Task 4；`register_defaults(model=None)` + kernel 集成（复用 BaseAgentRunner、commit_result 持久化）→ Task 5；data/code 不变、supervisor Manager 保留 → 全局约束（未改相关代码）。
- **占位符**：无 TBD/TODO；测试代码均为实际可执行内容。
- **类型一致**：`Agent(..., output_type=, artifacts=)`、`ResponsesProvider.stream(..., output_type=)`、`create_agent_for(...)`、三个工具类名与测试一致；`Agent` 实例属性命名为 `_output_type`/`_artifacts`（`vars(agent)` 断言在 Task 2 Step 1 同步更新）。

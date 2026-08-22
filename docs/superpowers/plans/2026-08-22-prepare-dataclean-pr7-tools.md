# PREPARE 阶段 dataclean agent + PR7 工具移植 + 全 PREPARE E2E 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 PREPARE 阶段新增 dataclean agent（evaluator 之后、EDA 之前），把 PR7 的 HuggingFace/MCP 工具移植进 dataclean / evaluator / baseline_ideator 的 extra_tools，并补一个走完整 PREPARE 的 E2E 测试。

**Architecture:** dataclean 复用 evaluator 的"PlanDecision + create/followup 修复循环"形态（`run_dataclean_plan`），完成 gate 是数据无关的——只检查 `workspaces/dataclean/DATACLEAN_HANDOFF.md` 非空 + `submit`，不校验任何固定产物布局。PR7 工具以 `src/athena/tools` 包整体移植；`ResearchRuntime` 加 `hf_tools()` / `init_mcp_tools()` / `agent_tools()` / `baseline_ideator_tools()` 四个装配方法（MCP 本期休眠：仓库根放空 `mcp_servers.json`，`init_mcp_tools` 是 no-op）。E2E 用 agent-aware 假 LLM 客户端驱动真实组合根，跑完 evaluator → dataclean → EDA → baseline_ideator → prepare。

**Tech Stack:** Python 3.11+、pytest-asyncio、`huggingface_hub`、`mcp>=1.0.0`、`httpx>=0.27.0`、uv（evaluator freeze）。

**Spec:** `docs/superpowers/specs/2026-08-22-prepare-dataclean-pr7-tools-design.md`（本计划逐条落实该设计；执行者需同时读 spec 与计划）

## Global Constraints

（每任务的实现隐式包含本节全部要求，来自 spec「工程约束」与用户明示）

- 遵循 `docs/代码规范.md`：import 只在顶部（stdlib → 第三方 → 项目）；无 `from __future__ import annotations`；公开函数/类要有 docstring；无 ASCII 装饰分隔线；嵌套不超过 3 层；为避循环依赖的延迟导入必须带 `# 延迟导入避免循环依赖` 注释。
- 代码轻量、工程化、低维护成本：避免复杂、冗余实现；禁止 YAGNI 式扩展。
- 避免在已有文件里大量改动（协作方共同维护）：只做最小必要改动——`phase_runner.py` 只加"步骤 1b"一小段 + EDA content 追加一行 + 两处 `extra_tools` 替换；`prepare.py` 只加一个函数 + 两个常量；`runtime.py` 只加装配方法；`register_prompt_agent` 不改。
- dataclean 完成 gate **数据无关**：只认 `DATACLEAN_HANDOFF.md` 存在且非空 + `decision == "submit"`。不校验固定文件/目录名、不解析 handoff 内容、不按数据集类型特判。
- dataclean 在 PREPARE 中的位置：**evaluator 冻结之后、EDA 之前**；断点复用 = `workspaces/dataclean/DATACLEAN_HANDOFF.md` 已存在。
- MCP 只移植工具层，**本期不配置 server**：仓库根放 `mcp_servers.json` = `{"servers": []}`；`init_mcp_tools` 幂等且今日为 no-op；`McpSearchTools` 的 `bind_to_registry` 钩子只记 docstring/TODO，不实现。
- 只给 **baseline_ideator** 加 HF+MCP（SEARCH ideators 的 `ideator_tools()` 保持不变）。
- 依赖版本下限沿用 pr7_review：`huggingface_hub>=0.20`、`mcp>=1.0.0`、`httpx>=0.27.0`。
- E2E：`@pytest.mark.slow`（默认 `-m 'not slow'` 不跑）；关闭 Kaggle、用本地合成 MAGFiLO 形状数据；dataclean 演示一个"坏产物 → 反馈 → 修复 → 提交"回合。
- 测试基建沿用现有接缝：`test/unit/_support.py` 的 `_text_chunk` / `_tool_chunk` / `_finish_chunk`、`make_project`；`test_prepare_agent_contract.py` 的 provider 动作队列模式。

---

### Task 1: 加入 PR7 依赖并安装

**Files:**
- Modify: `pyproject.toml`（`[project].dependencies`，行 `"kagglehub>=1.0.2",` 之后）
- No test file（纯装配步骤，被 Task 2 起的测试隐式验证）

**Interfaces:**
- Produces: 环境里可 `import huggingface_hub`、`import mcp`、`import httpx`（`mcp` 与 `httpx` 已装，缺 `huggingface_hub`）。

- [ ] **Step 1: 修改 pyproject.toml**

在 `dependencies` 列表末尾（`"kagglehub>=1.0.2",` 之后）加三行：

```toml
    "huggingface_hub>=0.20",
    "mcp>=1.0.0",
    "httpx>=0.27.0",
```

- [ ] **Step 2: 安装到 .venv**

Run: `uv sync`（仓库根）

Expected: 成功，输出包含 `huggingface_hub`、`mcp`、`httpx`。

- [ ] **Step 3: 验证可导入**

Run: `uv run python -c "import huggingface_hub, mcp, httpx; print('ok')"`

Expected: 打印 `ok`。

- [ ] **Step 4: 提交**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add huggingface_hub, mcp, httpx for PR7 tools port"
```

---

### Task 2: 移植 HuggingFace 工具（hf_dataset / hf_model）

**Files:**
- Create: `src/athena/tools/__init__.py`
- Create: `src/athena/tools/hf_dataset.py`
- Create: `src/athena/tools/hf_model.py`
- Create: `test/unit/tools/__init__.py`
- Create: `test/unit/tools/test_hf_dataset.py`
- Create: `test/unit/tools/test_hf_model.py`

**Interfaces:**
- Consumes: 源码在 `pr7_review/src/athena/tools/hf_dataset.py`、`hf_model.py`；测试在 `pr7_review/test/unit/tools/test_hf_dataset.py`、`test_hf_model.py`（其 import `athena.tools.hf_dataset` / `athena.tools.hf_model` 与 main 移植后路径一致）。
- Produces: `HFDatasetSearchTool` / `HFDatasetDownloadTool` / `HFModelSearchTool` / `HFModelDownloadTool`（构造器 `work_root="work"`，`output_dir = Path(work_root)/hf_*_search|download`；`_get_hf_api()` 模块级懒初始化）。Task 4 的 `hf_tools()` 消费这四个类。

- [ ] **Step 1: 写失败测试（先拷贝 pr7 测试）**

```bash
mkdir -p src/athena/tools test/unit/tools
touch src/athena/tools/__init__.py test/unit/tools/__init__.py
cp pr7_review/src/athena/tools/hf_dataset.py src/athena/tools/hf_dataset.py
cp pr7_review/src/athena/tools/hf_model.py src/athena/tools/hf_model.py
cp pr7_review/test/unit/tools/test_hf_dataset.py test/unit/tools/test_hf_dataset.py
cp pr7_review/test/unit/tools/test_hf_model.py test/unit/tools/test_hf_model.py
```

- [ ] **Step 2: 运行测试确认通过（cp 后即绿，非红绿红）**

Run: `uv run pytest test/unit/tools/test_hf_dataset.py test/unit/tools/test_hf_model.py -v`

Expected: 全部 PASS。这些是 pr7 里已绿的测试，import 路径与 main 移植后一致
（`athena.tools.hf_dataset` / `athena.tools.hf_model`），且只 mock
`HfApi.list_datasets` / `snapshot_download`，不联网。若意外报
`ModuleNotFoundError` 或指向缺失符号，按报错修正 `cp` 的来源路径。

- [ ] **Step 3: 移植裁剪——去掉终端 print 噪音与装饰注释**

`docs/代码规范.md` 不允许 `print` 噪音。对 `src/athena/tools/hf_dataset.py` 与 `src/athena/tools/hf_model.py` 做如下删除：

- 两个文件各自的 `_get_hf_api()` 里，删掉 `print(f"📡 HfApi initialized (endpoint={endpoint or 'default'})")` 这一行（`endpoint` 变量保留，供 `HfApi(endpoint=endpoint)` 使用）。
- `hf_dataset.py` 的 `HFDatasetSearchTool.execute` 里，删掉全部 `print(...)` 行（共 5 处：`🔍 Searching HuggingFace...`、`❌ Search failed...`、`✅ Found ...`、`⚠️  No results ... trying next query...`、`⚠️  No results ... no fallback...`）。

删除后 `_get_hf_api` 应为：

```python
def _get_hf_api() -> HfApi:
    """获取 HfApi 客户端，显式读取 HF_ENDPOINT 环境变量。"""
    global _hf_api
    if _hf_api is None:
        endpoint = os.environ.get("HF_ENDPOINT")
        _hf_api = HfApi(endpoint=endpoint)
    return _hf_api
```

另外把移植的 `test/unit/tools/test_hf_dataset.py` 里两处 `# ── ... ──` 装饰注释行删掉
（对齐代码规范，不影响行为）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/tools/test_hf_dataset.py test/unit/tools/test_hf_model.py -v`

Expected: 全部 PASS（这些测试 mock `HfApi.list_datasets` / `list_models` / `snapshot_download`，不联网）。

- [ ] **Step 5: 提交**

```bash
git add src/athena/tools/__init__.py src/athena/tools/hf_dataset.py src/athena/tools/hf_model.py test/unit/tools/__init__.py test/unit/tools/test_hf_dataset.py test/unit/tools/test_hf_model.py
git commit -m "feat(tools): port HuggingFace dataset/model search+download tools from pr7"
```

---

### Task 3: 移植 MCP 工具层

**Files:**
- Create: `src/athena/tools/mcp/__init__.py`
- Create: `src/athena/tools/mcp/config.py`
- Create: `src/athena/tools/mcp/client.py`
- Create: `src/athena/tools/mcp/adapter.py`
- Create: `src/athena/tools/mcp/search.py`
- Create: `test/unit/tools/mcp/__init__.py`
- Create: `test/unit/tools/mcp/test_config.py`
- Create: `test/unit/tools/mcp/test_client.py`
- Create: `test/unit/tools/mcp/test_adapter.py`
- Create: `test/unit/tools/mcp/test_search.py`

**Interfaces:**
- Consumes: 源码在 `pr7_review/src/athena/tools/mcp/*.py`；测试在 `pr7_review/test/unit/tools/mcp/*.py`。
- Produces: `load_mcp_servers(path) -> list[McpServerConfig]`（缺文件返回 `[]`）；`register_mcp_tools(registry, servers, *, work_root, max_discovered=30) -> list[McpClientManager]`（async，构建期连接钉住工具）；`McpClientManager`（懒连接，`async close()` / `async ensure_connected()` / `tool_defs()` / `full_name()`）。Task 4 的 `init_mcp_tools()` 消费 `load_mcp_servers` + `register_mcp_tools`，`aclose()` 消费 `McpClientManager.close()`。

- [ ] **Step 1: 写失败测试（先拷贝 pr7 测试）**

```bash
mkdir -p src/athena/tools/mcp test/unit/tools/mcp
touch src/athena/tools/mcp/__init__.py test/unit/tools/mcp/__init__.py
cp pr7_review/src/athena/tools/mcp/__init__.py src/athena/tools/mcp/__init__.py
cp pr7_review/src/athena/tools/mcp/config.py src/athena/tools/mcp/config.py
cp pr7_review/src/athena/tools/mcp/client.py src/athena/tools/mcp/client.py
cp pr7_review/src/athena/tools/mcp/adapter.py src/athena/tools/mcp/adapter.py
cp pr7_review/src/athena/tools/mcp/search.py src/athena/tools/mcp/search.py
cp pr7_review/test/unit/tools/mcp/test_config.py test/unit/tools/mcp/test_config.py
cp pr7_review/test/unit/tools/mcp/test_client.py test/unit/tools/mcp/test_client.py
cp pr7_review/test/unit/tools/mcp/test_adapter.py test/unit/tools/mcp/test_adapter.py
cp pr7_review/test/unit/tools/mcp/test_search.py test/unit/tools/mcp/test_search.py
```

- [ ] **Step 2: 运行测试确认通过（cp 后即绿，非红绿红）**

Run: `uv run pytest test/unit/tools/mcp -v`

Expected: 全部 PASS。pr7 的 MCP 测试只用 `AsyncMock` 注入 manager/session_factory，
不触发 adapter.py 里的 sandbox 打印分支（`_print_sandbox_*` 是自包含死代码，测试从不
调用）；`test_adapter.py` 从 `mcp.types` import `CallToolResult` 等，路径与 main 一致。
Step 3 的裁剪纯粹是对齐代码规范（去 print 噪音 + 去重复 return + 去死代码），不是修
测试失败。若意外报 `ModuleNotFoundError` 或缺失符号，按报错修正 `cp` 的来源路径。

- [ ] **Step 3: 移植裁剪 `src/athena/tools/mcp/adapter.py`**

对 `execute()` 方法与文件末尾做 4 处修改：

1. 删除 `execute()` 里这两行（含其注释）：
   ```python
   # 对 sandbox 工具打印执行信息到终端（stdout，与 demo 输出同流）
   _print_sandbox_input(self._mcp_tool.name, input)
   ```
   与
   ```python
   # 对 sandbox 工具打印返回结果到终端
   _print_sandbox_result(self._mcp_tool.name, payload)
   ```
2. 删除 `execute()` 错误分支里的终端打印行：
   ```python
   print(f"\n⚠ MCP 工具 [{self.spec.name}] 连接/下载失败: {err_msg}")
   ```
   与 isError 分支里的：
   ```python
   print(f"\n⚠ MCP 工具 [{self.spec.name}] 返回错误: {raw_err}")
   if enhanced != raw_err:
       hint = enhanced.split("\n", 1)[1] if "\n" in enhanced else enhanced
       print(f"  {hint}")
   ```
   （`err_msg`、`enhanced` 变量仍被下方 `_persist` 使用，保留。）
3. 删除 `execute()` 末尾**重复且不可达**的第二个 `return self._persist({**payload, "files": saved_files}, success=True)`，只保留第一个。
4. 删除文件末尾的沙箱打印辅助（保留 `_preview` 及之前的内容）：
   ```python
   _SANDBOX_TOOLS = {"python_inspect", "python_execute"}
   _SEP = "─" * 50
   def _print_sandbox_input(tool_name: str, args: dict) -> None: ...
   def _print_sandbox_result(tool_name: str, payload: dict) -> None: ...
   ```

`execute()` 正确结尾应只有一次 `return self._persist({**payload, "files": saved_files}, success=True)`，且模块内不再出现 `print(`、`_SANDBOX_TOOLS`、`_SEP`。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/tools/mcp -v`

Expected: 全部 PASS（这些测试用 `AsyncMock` 注入 session_factory / manager，不联网）。若某个测试引用了被裁剪的符号，按报错删除该断言（裁剪掉的只是终端打印，不影响行为断言）。

- [ ] **Step 5: 提交**

```bash
git add src/athena/tools/mcp test/unit/tools/mcp
git commit -m "feat(tools): port MCP tool layer from pr7 (dormant; no server config)"
```

---

### Task 4: ResearchRuntime 装配 HF / MCP 工具

**Files:**
- Create: `mcp_servers.json`（仓库根，空配置）
- Modify: `src/athena/research/runtime.py`
  - 顶部 import 区：加 `from athena.tools.hf_dataset import HFDatasetDownloadTool, HFDatasetSearchTool` 与 `from athena.tools.hf_model import HFModelDownloadTool, HFModelSearchTool` 与 `from athena.tools.mcp import register_mcp_tools` 与 `from athena.tools.mcp.client import McpClientManager` 与 `from athena.tools.mcp.config import load_mcp_servers`
  - `__init__` 字段区（约 `self._auto_seed_task = auto_seed_task` 附近）：加 `self._mcp_registry: ToolRegistry | None = None` 与 `self._mcp_managers: list[McpClientManager] = []`
  - 新增方法（放在 `kaggle_tools` / `ideator_tools` / `corpus_tools` 附近）：`hf_tools()`、`async init_mcp_tools()`、`agent_tools(agent_type)`、`baseline_ideator_tools()`
  - `aclose()`：在 `await self._agents.aclose()` 前关闭 MCP managers
- Test: `test/unit/research/test_runtime_pr7_tools.py`

**Interfaces:**
- Consumes: Task 2 的四个 HF 工具类、Task 3 的 `register_mcp_tools` / `load_mcp_servers` / `McpClientManager`；已有 `_merged(*registries)`（runtime.py:135）与 `kaggle_tools` / `corpus_tools` / `ideator_tools`。
- Produces（Task 7 消费）:
  - `hf_tools() -> ToolRegistry`（同步，HF 工具绑定 `self._root / "workspaces" / "pr7_tools"`）
  - `async init_mcp_tools() -> None`（幂等；从 `self._root / "mcp_servers.json"` 读配置，空则 no-op；缓存进 `_mcp_registry` / `_mcp_managers`）
  - `agent_tools(agent_type: str) -> ToolRegistry | None` = `_merged(kaggle_tools(agent_type), hf_tools(), _mcp_registry)`
  - `baseline_ideator_tools() -> Callable[[], ToolRegistry | None]`（懒 provider）= `_merged(kaggle_tools("ideator"), corpus_tools(for_ideation=True), hf_tools(), _mcp_registry)`

- [ ] **Step 1: 写失败测试**

Create `test/unit/research/test_runtime_pr7_tools.py`:

```python
"""ResearchRuntime 的 PR7 工具装配（HF / MCP）测试。"""

import pytest

from test.unit._support import make_project

HF_NAMES = {
    "hf_dataset_search",
    "hf_dataset_download",
    "hf_model_search",
    "hf_model_download",
}


def _names(registry) -> set[str]:
    return {spec.name for spec in registry.specs}


@pytest.mark.asyncio
async def test_hf_tools_bound_to_project(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        registry = rt.hf_tools()
        assert _names(registry) == HF_NAMES
        # 绑定到项目工作区而非 cwd：每个工具的落盘目录都在项目 pr7_tools 下
        pr7_root = str(tmp_path / "workspaces" / "pr7_tools")
        for name in HF_NAMES:
            assert str(registry.resolve(name).output_dir).startswith(pr7_root)
    finally:
        await rt.aclose()


@pytest.mark.asyncio
async def test_init_mcp_tools_noop_without_config(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        await rt.init_mcp_tools()
        assert rt._mcp_registry is not None
        assert _names(rt._mcp_registry) == set()
        assert rt._mcp_managers == []
        await rt.init_mcp_tools()  # 幂等：二次调用不重连
        assert _names(rt._mcp_registry) == set()
    finally:
        await rt.aclose()


@pytest.mark.asyncio
async def test_agent_tools_merges_hf_when_kaggle_off(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        registry = rt.agent_tools("evaluator")
        assert registry is not None
        assert _names(registry) == HF_NAMES
    finally:
        await rt.aclose()


@pytest.mark.asyncio
async def test_baseline_ideator_tools_is_lazy_and_includes_hf(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        provider = rt.baseline_ideator_tools()
        assert callable(provider)
        registry = provider()
        assert registry is not None
        assert _names(registry) == HF_NAMES
    finally:
        await rt.aclose()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/unit/research/test_runtime_pr7_tools.py -v`

Expected: FAIL——`AttributeError: 'ResearchRuntime' object has no attribute 'hf_tools'`。

- [ ] **Step 3: 实现 runtime 装配**

3a. 创建 `mcp_servers.json`（仓库根）：

```json
{"servers": []}
```

3b. `runtime.py` 加 import（放在现有第三方 import 之后、项目 import 区；`from athena.tools...` 属于项目内 import，按字母序插到现有项目 import 里）：

```python
from athena.tools.hf_dataset import HFDatasetDownloadTool, HFDatasetSearchTool
from athena.tools.hf_model import HFModelDownloadTool, HFModelSearchTool
from athena.tools.mcp import register_mcp_tools
from athena.tools.mcp.client import McpClientManager
from athena.tools.mcp.config import load_mcp_servers
```

3c. `__init__` 字段区加（紧邻 `self._auto_seed_task = auto_seed_task`）：

```python
        self._mcp_registry: ToolRegistry | None = None
        self._mcp_managers: list[McpClientManager] = []
```

3d. 新增四个方法（放 `ideator_tools()` / `corpus_tools()` 附近）：

```python
    def hf_tools(self) -> ToolRegistry:
        """装配 4 个 HuggingFace 搜索/下载工具，产物绑定到项目工作区。"""
        work_root = str(self._root / "workspaces" / "pr7_tools")
        registry = ToolRegistry()
        for tool_cls in (
            HFDatasetSearchTool,
            HFDatasetDownloadTool,
            HFModelSearchTool,
            HFModelDownloadTool,
        ):
            registry.register(tool_cls(work_root=work_root))
        return registry

    async def init_mcp_tools(self) -> None:
        """从项目根 mcp_servers.json 装配 MCP 工具；幂等，空配置为 no-op。

        本期 MCP 休眠：仓库根只有空配置，因此这是廉价 no-op。将来接入 server 时，
        ``McpSearchTools`` 的 ``bind_to_registry`` 钩子仍未实现——它绑定 registry 于
        构造期，经 ``extra_tools`` 拷贝语义的 agent 看不到动态激活的工具（见 spec B3）。
        """
        if self._mcp_registry is not None:
            return
        self._mcp_registry = ToolRegistry()
        self._mcp_managers = []
        servers = load_mcp_servers(self._root / "mcp_servers.json")
        if not servers:
            return
        self._mcp_managers = await register_mcp_tools(
            self._mcp_registry,
            servers,
            work_root=str(self._root / "workspaces" / "pr7_tools"),
        )

    def agent_tools(self, agent_type: str) -> ToolRegistry | None:
        """装配 PREPARE 相关 agent 的领域工具：Kaggle + HuggingFace + 缓存的 MCP。"""
        return _merged(
            self.kaggle_tools(agent_type),
            self.hf_tools(),
            self._mcp_registry,
        )

    def baseline_ideator_tools(self) -> Callable[[], ToolRegistry | None]:
        """baseline_ideator 的懒加载领域工具：Kaggle + 语料 + HF + 缓存的 MCP。"""
        def _provider() -> ToolRegistry | None:
            return _merged(
                self.kaggle_tools("ideator"),
                self.corpus_tools(for_ideation=True),
                self.hf_tools(),
                self._mcp_registry,
            )
        return _provider
```

（`Callable` 与 `ToolRegistry` 的 import 若不存在，按代码规范在顶部 import 区补 `from collections.abc import Callable`；`ToolRegistry` 已由 `from athena.core.tool import ToolRegistry` 提供。）

3e. `aclose()` 在 `await self._agents.aclose()` 之前加：

```python
        for mgr in self._mcp_managers:
            await mgr.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/research/test_runtime_pr7_tools.py -v`

Expected: 4 个测试全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add mcp_servers.json src/athena/research/runtime.py test/unit/research/test_runtime_pr7_tools.py
git commit -m "feat(runtime): assemble HF/MCP tools into agent_tools + baseline_ideator_tools"
```

---

### Task 5: dataclean agent 模块 + 数据无关 prompt

**Files:**
- Create: `src/athena/agents/prepare_dataclean_agent.py`
- Create: `src/athena/core/agent/prompts/dataclean_agent.md`
- Test: `test/unit/agent/test_prepare_dataclean_agent.py`

**Interfaces:**
- Consumes: `register_prompt_agent`（prompt_agent.py:106）、`PlanDecision`（supervisor/plans.py）、`AgentTypeRegistry`、`ArtifactStore`、`ToolRegistry`、`ExecutionRuntime`——全部与 `prepare_agent.py` 里 `register_evaluator_agent` 同源。
- Produces（Task 6/7/8 消费）:
  - `DATACLEAN_AGENT_ID = "dataclean"`、`DATACLEAN_AGENT_TYPE = "dataclean"`
  - `register_dataclean_agent(registry, *, provider, artifacts, workspace, runtime, extra_tools=None) -> None`——签名与 `register_evaluator_agent` 完全同构，`output_type=PlanDecision`，prompt 文件 `dataclean_agent.md`。

- [ ] **Step 1: 写失败测试**

Create `test/unit/agent/test_prepare_dataclean_agent.py`:

```python
"""dataclean agent 注册与数据无关 prompt 的单元测试。"""

from pathlib import Path

import pytest

from athena.agents.prepare_dataclean_agent import (
    DATACLEAN_AGENT_ID,
    DATACLEAN_AGENT_TYPE,
    register_dataclean_agent,
)
from athena.agents.prompt_agent import load_prompt
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore


def test_dataclean_registration_and_data_agnostic_prompt(tmp_path: Path) -> None:
    registry = AgentTypeRegistry()
    register_dataclean_agent(
        registry,
        provider=object(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        workspace=tmp_path / "dataclean",
        runtime=None,
    )
    assert registry.contains(DATACLEAN_AGENT_TYPE)

    prompt = load_prompt(DATACLEAN_AGENT_TYPE)
    for marker in ("INSPECT", "DECIDE", "IMPLEMENT", "DOCUMENT", "DATACLEAN_HANDOFF.md", "PlanDecision"):
        assert marker in prompt, f"prompt missing marker {marker!r}"
    # 数据无关：prompt 不得写死任何数据集布局
    assert "filament" not in prompt.lower()
    assert "coco" not in prompt.lower()


def test_dataclean_constants() -> None:
    assert DATACLEAN_AGENT_ID == "dataclean"
    assert DATACLEAN_AGENT_TYPE == "dataclean"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/unit/agent/test_prepare_dataclean_agent.py -v`

Expected: FAIL——`ModuleNotFoundError: athena.agents.prepare_dataclean_agent`。

- [ ] **Step 3: 实现模块与 prompt**

3a. Create `src/athena/agents/prepare_dataclean_agent.py`（镜像 `prepare_agent.py` 的 import 与结构）：

```python
"""DataClean PREPARE agent：分析给定数据并按实际情况清洗。"""

from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.plans import PlanDecision

DATACLEAN_AGENT_ID = "dataclean"
DATACLEAN_AGENT_TYPE = "dataclean"


def register_dataclean_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh DataClean Agent factory that writes a cleaning handoff.

    ``extra_tools`` 追加领域工具（Kaggle / HuggingFace / MCP），让 dataclean 能检索
    相似数据与预训练模型。完成 gate 由 ``run_dataclean_plan`` 按数据无关规则校验。
    """
    register_prompt_agent(
        registry,
        agent_type=DATACLEAN_AGENT_TYPE,
        output_type=PlanDecision,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


__all__ = [
    "DATACLEAN_AGENT_ID",
    "DATACLEAN_AGENT_TYPE",
    "register_dataclean_agent",
]
```

3b. Create `src/athena/core/agent/prompts/dataclean_agent.md`（数据无关方法论，全内容）：

```markdown
# DataClean Agent

你负责分析给定数据集并按该数据集的实际状况选择清洗方法。你可能需要编写并执行代码，
工作目录是你的工作区。**严禁假设数据集的结构**：一切结论以你实际勘察到的证据为准，
个性化决定属于你。

## 方法论：INSPECT → DECIDE → IMPLEMENT + RUN → DOCUMENT

1. 勘察（INSPECT）：枚举数据集的组成（文件、大小、格式）；读取 schema / 元数据
   （表头、标签文件、标注结构）；抽样读内容；量化问题（缺失、重复、损坏文件、标签
   语法不一致、越界值、类别失衡、格式不匹配）。不许跳过勘察直接拍脑袋。
2. 决策（DECIDE）：基于勘察证据选择清洗方法（丢弃/填补、去重、重格式化、重采样、
   过滤劣质记录、修复引用等），每条决策必须引用证据。数据本身已干净时明确说
   "无需清洗"，不要硬造工作。
3. 实现并运行（IMPLEMENT + RUN）：把清洗写成可复现、依赖轻的脚本（如 `clean.py`）
   放进工作区并执行；验证清洗后的数据能被正确读回。
4. 自描述（DOCUMENT）：写 `DATACLEAN_HANDOFF.md`——数据概览、发现的问题、逐条决策
   与理由、产出布局（文件 + 格式）、下游如何消费清洗后的数据、如何复现清洗脚本。
   **不允许声称没有真正执行的清洗。**

## 输出

最后以 `PlanDecision` JSON 提交：

- `continue`：还需要更多轮次完成清洗（你会收到反馈后继续）。
- `submit`：`DATACLEAN_HANDOFF.md` 已存在且如实描述了实际完成的工作。
- `abandon`：数据无法清洗或任务无意义（会导致计划中止）。

当 `DATACLEAN_HANDOFF.md` 缺失或为空时，提交会被打回并要求你补全。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/agent/test_prepare_dataclean_agent.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/agents/prepare_dataclean_agent.py src/athena/core/agent/prompts/dataclean_agent.md test/unit/agent/test_prepare_dataclean_agent.py
git commit -m "feat(agents): add DataClean agent with data-agnostic cleaning methodology"
```

---

### Task 6: run_dataclean_plan 修复循环 + Kaggle wiring + 单 agent 契约测试

**Files:**
- Modify: `src/athena/research/supervisor/prepare.py`
  - 常量区（`EVALUATOR_PLAN_ID = "evaluator"` 之后）：加 `DATACLEAN_AGENT_ID = "dataclean"` 与 `DATACLEAN_PLAN_ID = "dataclean"`
  - 新增 `run_dataclean_plan`（放在 `run_evaluator_plan` 之后、`run_prepare_plan` 之前）
- Modify: `src/athena/kaggle/wiring.py`——`AGENT_KAGGLE_TOOLS` 加 `"dataclean"` 条目
- Test: `test/integration/research/test_dataclean_agent_contract.py`

**Interfaces:**
- Consumes: Task 5 的 `register_dataclean_agent` / `DATACLEAN_AGENT_TYPE`；已有 `_decision_from_summary` / `wait_run_events` / `PlanDecision` / `AgentRuntime` / `ArtifactStore` / `ExecutionRuntime`（均在 prepare.py 已 import）。
- Produces（Task 7 消费）:
  - `async run_dataclean_plan(*, agents: AgentRuntime, store: ArtifactStore, dataclean_dir: Path, execution: ExecutionRuntime, task: str, max_turns: int, publish: EmitEvent | None = None) -> str`——返回 `DATACLEAN_HANDOFF.md` 文本；gate = handoff 非空 + `submit`；`abandon` 抛错；turn budget 耗尽抛错。
  - `AGENT_KAGGLE_TOOLS["dataclean"] = (KAGGLE_GET_COMPETITION, KAGGLE_DOWNLOAD_DATA)`

- [ ] **Step 1: 写失败测试**

Create `test/integration/research/test_dataclean_agent_contract.py`:

```python
"""run_dataclean_plan 修复循环 + 数据无关完成 gate 的契约测试。"""

import json
from pathlib import Path

import pytest

from athena.agents.prepare_dataclean_agent import register_dataclean_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.prepare import run_dataclean_plan

HANDOFF_COCO = (
    "# DataClean Handoff\n"
    "Inspected the COCO annotations: 2 images, 1 polygon; no issues, data clean.\n"
)
HANDOFF_TABULAR = (
    "# DataClean Handoff\n"
    "Inspected train.csv: 20 rows, 5 cols; filled 2 missing cells, deduped 1 row.\n"
)


def _submit() -> str:
    return json.dumps({"decision": "submit", "reason": "clean", "suggestions": []})


def _abandon() -> str:
    return json.dumps({"decision": "abandon", "reason": "no data", "suggestions": []})


class _DataCleanProvider:
    """动作队列 provider：每 stream 一个 write_file / submit / abandon。

    action 取值：``None`` → submit 答案；``"abandon"`` → abandon 答案；否则
    ``(path, content)`` → write_file。沿用 ``_EvaluatorProvider`` 语义：一次 plan
    迭代内部会跑多轮 stream（write_file 执行后继续），直到 provider 交出文本答案。
    """

    model_name = "dataclean-integration"

    def __init__(self) -> None:
        self.calls = 0
        self._actions: list[tuple[str, str] | None | str] = []

    def set_actions(self, actions: list[tuple[str, str] | None | str]) -> None:
        self._actions = list(actions)

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        self.calls += 1
        action = self._actions.pop(0)
        if isinstance(action, str):
            answer = _abandon() if action == "abandon" else _submit()
            yield StreamEvent(
                kind="text_delta", data={"delta": answer, "accumulated": answer}
            )
        else:
            path, content = action
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"write-{self.calls}",
                    "name": "write_file",
                    "arguments": {"path": path, "content": content},
                },
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store = LocalArtifactStore(tmp_path / "artifacts")
        self.dataclean_dir = tmp_path / "dataclean"
        self.runtime = ExecutionRuntime(
            project_root=self.dataclean_dir, store=self.store
        )
        self.provider = _DataCleanProvider()

    async def run(self, *, max_turns: int = 4, task: str = "clean the data") -> str:
        registry = AgentTypeRegistry()
        register_dataclean_agent(
            registry,
            provider=self.provider,
            artifacts=self.store,
            workspace=self.dataclean_dir,
            runtime=self.runtime,
        )
        agents = AgentRuntime(
            type_registry=registry,
            project_root=self.tmp_path,
            rollout_dir=self.tmp_path / ".athena" / "logs" / "agents",
        )
        agents.start()
        try:
            return await run_dataclean_plan(
                agents=agents,
                store=self.store,
                dataclean_dir=self.dataclean_dir,
                execution=self.runtime,
                task=task,
                max_turns=max_turns,
            )
        finally:
            await agents.aclose()


@pytest.mark.asyncio
async def test_dataclean_plan_submits_with_handoff(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.provider.set_actions(
        [("clean.py", "print('cleaned')\n"), ("DATACLEAN_HANDOFF.md", HANDOFF_COCO), None]
    )
    text = await harness.run()
    assert text == HANDOFF_COCO
    handoff = harness.dataclean_dir / "DATACLEAN_HANDOFF.md"
    assert handoff.read_text(encoding="utf-8") == HANDOFF_COCO


@pytest.mark.asyncio
async def test_dataclean_plan_repairs_missing_handoff(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    # 首轮只写 clean.py 就 submit → gate 打回 → 补 handoff → 再 submit
    harness.provider.set_actions(
        [
            ("clean.py", "print('cleaned')\n"),
            None,
            ("DATACLEAN_HANDOFF.md", HANDOFF_COCO),
            None,
        ]
    )
    text = await harness.run()
    assert text == HANDOFF_COCO
    # 修复轮确实发生：agent 提交了两次
    assert harness.provider.calls == 4


@pytest.mark.asyncio
async def test_dataclean_plan_abandon_raises(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.provider.set_actions(["abandon"])
    with pytest.raises(RuntimeError, match="abandoned"):
        await harness.run()


@pytest.mark.asyncio
async def test_dataclean_plan_budget_exhausted(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    # 一直不写 handoff，max_turns=1 → budget 耗尽
    harness.provider.set_actions([("clean.py", "print('cleaned')\n"), None])
    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await harness.run(max_turns=1)


@pytest.mark.parametrize("handoff", [HANDOFF_COCO, HANDOFF_TABULAR])
@pytest.mark.asyncio
async def test_dataclean_gate_is_dataset_agnostic(
    tmp_path: Path, handoff: str
) -> None:
    # 两种形状完全不同（COCO 标注 vs 纯表格），gate 只认“非空 handoff + submit”
    harness = _Harness(tmp_path)
    harness.provider.set_actions(
        [("clean.py", "print('cleaned')\n"), ("DATACLEAN_HANDOFF.md", handoff), None]
    )
    text = await harness.run(task=f"clean {handoff.splitlines()[0]}")
    assert text == handoff
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/integration/research/test_dataclean_agent_contract.py -v`

Expected: FAIL——`ImportError: cannot import name 'run_dataclean_plan'`（且 abandon 用例会因 provider 动作耗尽而 `IndexError`；先看 import 错误）。

- [ ] **Step 3: 实现 run_dataclean_plan + wiring**

3a. `prepare.py` 常量区加：

```python
DATACLEAN_AGENT_ID = "dataclean"
DATACLEAN_PLAN_ID = "dataclean"
```

3b. `prepare.py` 在 `run_evaluator_plan` 之后加（镜像其修复循环，gate 数据无关）：

```python
async def run_dataclean_plan(
    *,
    agents: AgentRuntime,
    store: ArtifactStore,
    dataclean_dir: Path,
    execution: ExecutionRuntime,
    task: str,
    max_turns: int,
    publish: EmitEvent | None = None,
) -> str:
    """Run and repair one DataClean Agent until a non-empty handoff exists."""

    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    root = Path(dataclean_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    execution.ensure_environment()

    context_ref = await store.put_text(
        json.dumps(
            {"plan_id": DATACLEAN_PLAN_ID, "task": task},
            ensure_ascii=False,
        )
    )
    agent_id, run_id = await agents.create_root(
        "dataclean",
        {"content": task, "context_refs": [context_ref]},
        agent_id=DATACLEAN_AGENT_ID,
        name=DATACLEAN_PLAN_ID,
    )
    if agent_id != DATACLEAN_AGENT_ID:
        raise RuntimeError(f"dataclean Agent id must be {DATACLEAN_AGENT_ID}")

    handoff_path = root / "DATACLEAN_HANDOFF.md"
    feedback: str | None = None
    for turn in range(max_turns):
        if turn:
            run_id = await agents.followup(
                DATACLEAN_AGENT_ID,
                {"content": feedback, "context_refs": []},
            )
        summary = await wait_run_events(agents, run_id, publish)
        try:
            decision = await _decision_from_summary(summary, store)
        except (OSError, RuntimeError, ValueError) as exc:
            feedback = (
                "previous dataclean turn did not produce a valid decision: "
                f"{' '.join(str(exc).split())[:1000]}"
            )
            continue
        if decision.decision == "abandon":
            raise RuntimeError(
                f"dataclean Agent abandoned the cleaning pass: {decision.reason}"
            )
        # 数据无关的完成 gate：只认“非空 handoff + submit”，不校验任何固定产物布局。
        # 校验失败转 feedback 继续修复循环（镜像 run_evaluator_plan 的 try/except）。
        try:
            if not (
                handoff_path.is_file()
                and handoff_path.read_text(encoding="utf-8").strip()
            ):
                raise ValueError(
                    "DATACLEAN_HANDOFF.md is missing or empty. Inspect the data, "
                    "apply whatever cleaning you decide is needed (or none if it "
                    "is already clean), write DATACLEAN_HANDOFF.md describing what "
                    "you found and did, and return submit when done."
                )
            if decision.decision != "submit":
                raise ValueError(
                    "DATACLEAN_HANDOFF.md exists but the decision was "
                    f"{decision.decision!r}. Return submit to advance to EDA."
                )
            return handoff_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            feedback = " ".join(str(exc).split())[:1000]

    raise RuntimeError("dataclean turn budget exhausted without a cleaning handoff")
```

3c. `kaggle/wiring.py` 的 `AGENT_KAGGLE_TOOLS` 在 `"evaluator": (KAGGLE_GET_COMPETITION, KAGGLE_DOWNLOAD_DATA),` 之后加：

```python
    "dataclean": (KAGGLE_GET_COMPETITION, KAGGLE_DOWNLOAD_DATA),
```

3d. `prepare.py` 底部 `__all__` 追加：

```python
    "DATACLEAN_AGENT_ID",
    "DATACLEAN_PLAN_ID",
    "run_dataclean_plan",
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/integration/research/test_dataclean_agent_contract.py -v`

Expected: 全部 PASS（4 个用例 + 1 个参数化 ×2）。

- [ ] **Step 5: 提交**

```bash
git add src/athena/research/supervisor/prepare.py src/athena/kaggle/wiring.py test/integration/research/test_dataclean_agent_contract.py
git commit -m "feat(prepare): add data-agnostic run_dataclean_plan repair loop + kaggle wiring"
```

---

### Task 7: phase_runner 接入 dataclean 步骤 + 排序测试

**Files:**
- Modify: `src/athena/research/phase_runner.py`
  - import 区：`from athena.agents.prepare_dataclean_agent import DATACLEAN_AGENT_TYPE, register_dataclean_agent`；`from athena.research.supervisor.prepare import` 追加 `run_dataclean_plan`
  - `run_prepare_phase`：步骤 1 之后、步骤 2a 之前插入"步骤 1b"
  - 顶部加一次 `await rt.init_mcp_tools()`
  - 两处 `extra_tools` 替换：evaluator `rt.kaggle_tools("evaluator")` → `rt.agent_tools("evaluator")`；baseline_ideator `rt.ideator_tools()` → `rt.baseline_ideator_tools()`
  - EDA orchestrator 的 `content` 追加一行清洗数据说明
- Test: `test/integration/research/test_prepare_phase_dataclean_order.py`

**Interfaces:**
- Consumes: Task 6 的 `run_dataclean_plan`、Task 4 的 `rt.agent_tools` / `rt.baseline_ideator_tools` / `rt.init_mcp_tools`、Task 5 的 `register_dataclean_agent`。
- Produces: 排序 `evaluator → dataclean → prepare_eda(EDA_TODO) → eda_todos → prepare_eda(finalize) → baseline_ideator → prepare`。

- [ ] **Step 1: 写失败测试**

Create `test/integration/research/test_prepare_phase_dataclean_order.py`:

```python
"""run_prepare_phase 中 dataclean 步骤位于 evaluator 之后、EDA 之前的排序测试。"""

import json
from pathlib import Path

import pytest

from test.unit._support import FakeProvider

from athena.core.agent import settings
from athena.research.phase_runner import PhaseRunner
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.prepare import PrepareResult


def _ref(hexchar: str) -> str:
    return "sha256:" + hexchar * 64


@pytest.mark.asyncio
async def test_dataclean_step_sits_between_evaluator_and_eda(
    tmp_path: Path, monkeypatch
) -> None:
    order: list[str] = []

    async def stub_run_evaluator_plan(**kwargs) -> str:
        order.append("evaluator")
        return await kwargs["store"].put_text(
            json.dumps({"bundle_id": "evaluator", "entrypoint": "evaluate.py"})
        )

    async def stub_run_dataclean_plan(**kwargs) -> str:
        order.append("dataclean")
        return "cleaned handoff text"

    async def stub_run_prepare_plan(**kwargs) -> PrepareResult:
        order.append("prepare")
        return PrepareResult(
            evaluator_ref=_ref("a"),
            metric=0.7,
            commit="deadbeef",
            predictions_ref=_ref("b"),
            evidence_ref=_ref("c"),
            report_ref=_ref("d"),
        )

    monkeypatch.setattr(
        "athena.research.phase_runner.run_evaluator_plan", stub_run_evaluator_plan
    )
    monkeypatch.setattr(
        "athena.research.phase_runner.run_dataclean_plan", stub_run_dataclean_plan
    )
    monkeypatch.setattr(
        "athena.research.phase_runner.run_prepare_plan", stub_run_prepare_plan
    )

    async def stub_handoff(
        self, *, agent_id, agent_type, workspace, output_file, content
    ):
        order.append(agent_id)
        path = Path(workspace) / output_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
        return "ok"

    monkeypatch.setattr(PhaseRunner, "_run_handoff_agent", stub_handoff)

    async def stub_run_eda_todos(**kwargs):
        order.append("eda_todos")
        return []

    monkeypatch.setattr(
        "athena.research.phase_runner.run_eda_todos", stub_run_eda_todos
    )

    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="predict survival",
        model=settings.model_name(),
        client=FakeProvider(),
    )
    try:
        await runtime._phase_runner.run_prepare_phase()
    finally:
        await runtime.aclose()

    assert order == [
        "evaluator",
        "dataclean",
        "prepare_eda",  # EDA_TODO
        "eda_todos",
        "prepare_eda",  # EDA finalize
        "baseline_ideator",
        "prepare",
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/integration/research/test_prepare_phase_dataclean_order.py -v`

Expected: FAIL——顺序里没有 `"dataclean"`（当前 phase_runner 未接入 dataclean 步骤），断言 `order == [...]` 报错。

- [ ] **Step 3: 实现 phase_runner 接线**

3a. import 区改动：

```python
from athena.agents.prepare_dataclean_agent import (
    DATACLEAN_AGENT_TYPE,
    register_dataclean_agent,
)
```

并把 `from athena.research.supervisor.prepare import (` 的成员列表追加 `run_dataclean_plan`（按字母序插到 `run_evaluator_plan,` 与 `run_prepare_plan,` 之间）：

```python
    run_dataclean_plan,
    run_evaluator_plan,
    run_prepare_plan,
```

3b. `run_prepare_phase` 顶部（`rt._provider is None` 检查之后、`await rt.publish_output(... PREPARE: 初始化项目仓库…)` 之前）加：

```python
        # MCP 工具装配：本期仓库根只有空配置，是幂等 no-op（见 spec B3）。
        await rt.init_mcp_tools()
```

3c. 在"步骤 1 evaluator 冻结"结束（`# 步骤 2a：EDA orchestrator → todo workers → finalize。` 注释之前）插入"步骤 1b"：

```python
        # 步骤 1b：dataclean agent 在 workspaces/dataclean/ 分析并清洗数据；
        # 以 DATACLEAN_HANDOFF.md 非空为断点，复用时不重跑。
        dataclean_dir = rt._workspaces_root / "dataclean"
        dataclean_handoff_path = dataclean_dir / "DATACLEAN_HANDOFF.md"
        dataclean_handoff: str | None = None
        if dataclean_handoff_path.is_file() and dataclean_handoff_path.read_text(
            encoding="utf-8"
        ).strip():
            dataclean_handoff = dataclean_handoff_path.read_text(encoding="utf-8")
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 复用 dataclean 断点，跳过 DataClean Agent。",
            )
        else:
            await rt.publish_output(
                source="supervisor",
                channel="text",
                text="PREPARE: 运行 DataClean Agent…",
            )
            if not rt._registry.contains(DATACLEAN_AGENT_TYPE):
                register_dataclean_agent(
                    rt._registry,
                    provider=rt._provider,
                    artifacts=rt._store,
                    workspace=dataclean_dir,
                    runtime=rt._execution,
                    extra_tools=rt.agent_tools("dataclean"),
                )
            dataclean_handoff = await run_dataclean_plan(
                agents=rt._agents,
                store=rt._store,
                dataclean_dir=dataclean_dir,
                execution=rt._execution,
                task=rt._task_text,
                max_turns=MAX_PLAN_TURNS,
                publish=lambda kind, ref, data: rt._events_bus.project_agent_event(
                    "dataclean", kind, ref, data
                ),
            )
```

3d. EDA orchestrator 第一次 `_run_handoff_agent` 的 `content=rt._task_text,` 改为：

```python
                content=rt._task_text
                + (
                    f"\n\nDataClean ran in {dataclean_dir}; read "
                    "DATACLEAN_HANDOFF.md there to consume the cleaned data."
                    if dataclean_handoff
                    else ""
                ),
```

3e. 两处 `extra_tools` 替换：

```python
                    extra_tools=rt.agent_tools("evaluator"),   # 原 rt.kaggle_tools("evaluator")
...
                    extra_tools=rt.baseline_ideator_tools(),   # 原 rt.ideator_tools()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/integration/research/test_prepare_phase_dataclean_order.py -v`

Expected: PASS。另跑一遍现有相关测试确认无回归：

Run: `uv run pytest test/integration/research/test_autonomous_research.py -v`

Expected: PASS（该测试用 `prepare_phase=` 桩，不依赖本任务的接线细节，但确认 `run_prepare_phase` 改动没破坏 import/签名）。

- [ ] **Step 5: 提交**

```bash
git add src/athena/research/phase_runner.py test/integration/research/test_prepare_phase_dataclean_order.py
git commit -m "feat(prepare): wire dataclean step after evaluator, before EDA"
```

---

### Task 8: 全 PREPARE E2E 测试

**Files:**
- Create: `test/integration/research/test_prepare_phase_e2e.py`（`@pytest.mark.slow`）

**Interfaces:**
- Consumes: Task 4–7 的全部接线；`test/unit/_support.py` 的 `_text_chunk` / `_tool_chunk` / `_finish_chunk`；`runtime.start_task(task)`、`runtime.state.phase`、`runtime.tree.get_experiment`；`settings.model_name()`。
- Produces: 一个模拟前端 prompt 走完整 PREPARE 的组合根测试（Kaggle 关闭 + 本地合成数据 + dataclean 一个修复轮）。

- [ ] **Step 1: 写失败测试（完整文件）**

Create `test/integration/research/test_prepare_phase_e2e.py`:

```python
"""E2E：前端 prompt 进入完整 PREPARE 阶段的组合根演练。

LLM 用 agent-aware 假客户端，编排 / 文件 IO / git / 评估脚本执行全真。
Kaggle 关闭，用本地合成 MAGFiLO 形状数据；dataclean 演示一个修复轮。
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

from test.unit._support import _finish_chunk, _text_chunk, _tool_chunk

from athena.core.agent import settings
from athena.research.runtime import ResearchRuntime

_SUBMIT = json.dumps(
    {"decision": "submit", "reason": "ok", "suggestions": []}, ensure_ascii=False
)


def _handoff_result(output_file: str) -> str:
    return json.dumps(
        {"summary": "done", "handoff_file": output_file}, ensure_ascii=False
    )


_EVAL_PY = """\
import json
import sys

def main():
    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')
    preds = {}
    with open("predictions/predictions.csv", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[0] != "__athena_row_id":
                preds[parts[0]] = float(parts[1])
    labels = {}
    with open("labels.csv", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[0] != "__athena_row_id":
                labels[parts[0]] = float(parts[1])
    rows = [(preds[k], labels[k]) for k in preds if k in labels]
    if not rows:
        raise RuntimeError("no aligned predictions")
    json.dump(
        {
            "primary": sum(1.0 for a, b in rows if a == b) / len(rows),
            "metric": "accuracy",
        },
        out,
    )

main()
"""

_LABELS_CSV = (
    "__athena_row_id,label\n"
    "20140609195854Bh.jpeg,1\n"
    "20140610195854Bh.jpeg,0\n"
)

_MODEL_PY = """\
from pathlib import Path
Path("predictions").mkdir(exist_ok=True)
Path("predictions/predictions.csv").write_text(
    "__athena_row_id,prediction\\n"
    "20140609195854Bh.jpeg,1\\n"
    "20140610195854Bh.jpeg,0\\n",
    encoding="utf-8",
)
Path("report.md").write_text("# Baseline\\n", encoding="utf-8")
"""

_CLEAN_PY = """\
from pathlib import Path
for p in Path(".").rglob("*.jpeg"):
    assert p.read_bytes()[:2] == b"\\xff\\xd8"
print("clean")
"""

_DATACLEAN_HANDOFF = """\
# DataClean Handoff

Inspected the local filament-segmentation-2026 directory (train_images + one
COCO annotations file + test_images). All images parse as JPEG and annotations
reference existing images. No rows dropped, no repairs needed; the data is
consumed as-is. Reproduce with clean.py in this workspace.
"""

_EDA_TODO = (
    "## Stage 0: Overview (parallel: true)\n"
    "- [ ] Read the cleaned data and write a report -> EDA_REPORT_00_OVERVIEW.md\n"
)


class _AgentScript:
    """一个 agent 的逐 turn 假剧本；每 turn 一个 tool call 或最终 JSON 文本。"""

    def __init__(self, turns: list[tuple[str, ...]]) -> None:
        self.turns = turns
        self.calls = 0
        self.submits = 0

    def next(self) -> list:
        self.calls += 1
        turn = self.turns[self.calls - 1]
        if turn[0] == "text":
            if '"decision": "submit"' in turn[1]:
                self.submits += 1
            return [_text_chunk(turn[1]), _finish_chunk("stop")]
        # call_id 跨轮次唯一，避免工具结果按 id 匹配时串扰
        return [_tool_chunk(self.calls, turn[1], turn[2]), _finish_chunk("tool_calls")]


def _system_prompt(messages) -> str:
    """拼接全部 system-prompt part 的内容。"""
    chunks: list[str] = []
    for m in messages:
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", "") == "system-prompt":
                content = getattr(p, "content", "")
                if isinstance(content, str):
                    chunks.append(content)
    return "\n".join(chunks)


def _user_content(messages) -> str:
    """返回最近一条 user-prompt part 的内容；EDA worker 无 user 提示时为 ''。"""
    for m in messages:
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", "") == "user-prompt":
                content = getattr(p, "content", "")
                if isinstance(content, str):
                    return content
    return ""


def _build_scripts(data_dir: Path) -> dict[str, _AgentScript]:
    supervisor = _AgentScript(
        [
            ("tool", "configure_kaggle", {"enabled": False, "download": False}),
            (
                "tool",
                "record_task_understanding",
                {
                    "title": "solar filament segmentation 2026",
                    "dataset": str(data_dir),
                    "target": "filament instance segmentation",
                    "task_type": "instance_segmentation",
                    "primary_metric": "map",
                    "direction": "maximize",
                    "evaluation_plan": (
                        "eval.py reads predictions/predictions.csv and labels.csv "
                        "aligned by __athena_row_id and computes mean average precision."
                    ),
                },
            ),
            ("text", json.dumps({"answer": "task understood"}, ensure_ascii=False)),
        ]
    )
    evaluator = _AgentScript(
        [
            (
                "tool",
                "write_file",
                {"path": "metric.json", "content": '{"eval_script": "evaluate.py"}\n'},
            ),
            (
                "tool",
                "write_file",
                {
                    "path": "pyproject.toml",
                    "content": "[project]\nname = 'eval'\nversion = '0.1.0'\n"
                    "requires-python = '>=3.11'\ndependencies = []\n",
                },
            ),
            ("tool", "write_file", {"path": "evaluate.py", "content": _EVAL_PY}),
            ("tool", "write_file", {"path": "labels.csv", "content": _LABELS_CSV}),
            (
                "tool",
                "write_file",
                {
                    "path": "HANDOFF.md",
                    "content": "# Eval contract\npredictions/predictions.csv "
                    "(__athena_row_id,prediction); accuracy over aligned ids\n",
                },
            ),
            ("text", _SUBMIT),
        ]
    )
    dataclean = _AgentScript(
        [
            ("tool", "write_file", {"path": "clean.py", "content": _CLEAN_PY}),
            ("text", _SUBMIT),  # 无 handoff → gate 打回 → 修复轮
            (
                "tool",
                "write_file",
                {"path": "DATACLEAN_HANDOFF.md", "content": _DATACLEAN_HANDOFF},
            ),
            ("text", _SUBMIT),
        ]
    )
    eda_orchestrator = _AgentScript(
        [
            ("tool", "write_file", {"path": "EDA_TODO.md", "content": _EDA_TODO}),
            ("text", _handoff_result("EDA_TODO.md")),
            ("tool", "write_file", {"path": "EDA_INDEX.md", "content": "# EDA Index\n"}),
            (
                "tool",
                "write_file",
                {"path": "EDA_HANDOFF.md", "content": "# EDA Handoff\n"},
            ),
            ("text", _handoff_result("EDA_INDEX.md")),
        ]
    )
    baseline_ideator = _AgentScript(
        [
            (
                "tool",
                "write_file",
                {"path": "BASELINE_DESIGN.md", "content": "# Baseline Design\n"},
            ),
            ("text", _handoff_result("BASELINE_DESIGN.md")),
        ]
    )
    prepare = _AgentScript(
        [
            (
                "tool",
                "write_file",
                {
                    "path": "solution/features.py",
                    "content": "def feature(value):\n    return int(value)\n",
                },
            ),
            ("tool", "write_file", {"path": "solution/model.py", "content": _MODEL_PY}),
            (
                "tool",
                "write_file",
                {
                    "path": "experiment.json",
                    "content": json.dumps(
                        {
                            "version": 1,
                            "commands": [["python", "solution/model.py"]],
                            "outputs": {
                                "predictions": "predictions",
                                "report": "report.md",
                            },
                        }
                    ),
                },
            ),
            ("text", _SUBMIT),
        ]
    )
    return {
        "# SupervisorAgent": supervisor,
        "# Evaluator Agent": evaluator,
        "# DataClean Agent": dataclean,
        "# PREPARE EDA Agent": eda_orchestrator,
        "# Baseline Ideator Agent": baseline_ideator,
        "# PREPARE Agent": prepare,
    }


class _FakeChat:
    def __init__(self, client: "FakePrepareClient") -> None:
        self.completions = _FakeCompletions(client)


class _FakeCompletions:
    def __init__(self, client: "FakePrepareClient") -> None:
        self._client = client

    async def create(self, **kwargs):
        self._client.calls += 1
        messages = kwargs.get("messages", [])
        script = self._client.dispatch(messages)

        async def stream():
            for chunk in script.next():
                yield chunk

        return stream()


class FakePrepareClient:
    """按 system prompt 的 agent 标记分发脚本的假客户端。

    分发规则：
    - system 含 "# PREPARE EDA Agent" 且有 user 内容 → EDA orchestrator；
    - 同 system 但无 user 内容 → eda-worker（spawn 的 task dict 没有 content 键，
      因此 worker 的 LLM 上下文只有 system prompt；其 output_file 与假 orchestrator
      写入的 EDA_TODO.md 约定一致）；
    - 其余按 marker 表分发，未命中即抛错。
    """

    def __init__(self, data_dir: Path) -> None:
        self.chat = _FakeChat(self)
        self.calls = 0
        self._scripts = _build_scripts(data_dir)
        self._worker = _AgentScript(
            [
                (
                    "tool",
                    "write_file",
                    {"path": "EDA_REPORT_00_OVERVIEW.md", "content": "# Report\n"},
                ),
                ("text", _handoff_result("EDA_REPORT_00_OVERVIEW.md")),
            ]
        )

    def dispatch(self, messages) -> _AgentScript:
        system = _system_prompt(messages)
        user = _user_content(messages)
        if "# PREPARE EDA Agent" in system:
            if user:
                return self._scripts["# PREPARE EDA Agent"]
            return self._worker
        for marker, script in self._scripts.items():
            if marker in system:
                return script
        raise AssertionError(f"E2E 假客户端遇到未知 agent：{system[:120]!r}")

    @property
    def dataclean_submits(self) -> int:
        return self._scripts["# DataClean Agent"].submits


def _seed_magfilo_like(tmp_path: Path) -> Path:
    """种一份形状贴合 MAGFiLO 的合成数据集（train_images + COCO + test_images）。"""
    data = tmp_path / "filament-segmentation-2026"
    train_imgs = data / "train" / "train_images"
    test_imgs = data / "test" / "test_images"
    train_imgs.mkdir(parents=True)
    test_imgs.mkdir(parents=True)
    for name in ("20140609195854Bh.jpeg", "20140610195854Bh.jpeg"):
        (train_imgs / name).write_bytes(b"\xff\xd8\xff\xd9")
    (test_imgs / "20150609195854Bh.jpeg").write_bytes(b"\xff\xd8\xff\xd9")
    coco = {
        "images": [
            {"id": "img-a", "file_name": "20140609195854Bh.jpeg",
             "width": 2048, "height": 2048},
            {"id": "img-b", "file_name": "20140610195854Bh.jpeg",
             "width": 2048, "height": 2048},
        ],
        "annotations": [
            {
                "id": "ann-1",
                "image_id": "img-a",
                "category_id": 1,
                "bbox": [1.0, 1.0, 10.0, 10.0],
                "area": 100.0,
                "iscrowd": 0,
                "segmentation": [
                    [1.0, 1.0, 1.0, 11.0, 11.0, 11.0, 11.0, 1.0]
                ],
            }
        ],
        "categories": [
            {"id": 1, "name": "Left", "supercategory": "filament"}
        ],
    }
    (data / "train" / "MAGFiLO_1.0_Annotations_kaggle2026_train.json").write_text(
        json.dumps(coco), encoding="utf-8"
    )
    return data


async def _eventually(predicate, timeout: float = 120) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_full_prepare_phase_e2e(tmp_path: Path) -> None:
    data_dir = _seed_magfilo_like(tmp_path)
    runtime = ResearchRuntime(
        project_root=tmp_path,
        model=settings.model_name(),
        client=FakePrepareClient(data_dir),
        search_limit=0,
        auto_validate=False,
    )
    try:
        task = f"我要参加 solar filament segmentation 2026 比赛，本地数据在 {data_dir}"
        await runtime.start_task(task)
        # PREPARE 跑完 → SEARCH（search_limit=0 且 auto_validate=False，停在 WAITING）
        await _eventually(lambda: runtime.state.phase == "SEARCH")

        root = tmp_path
        assert (root / "workspaces/evaluator/metric.json").is_file()
        assert (root / "workspaces/evaluator/evaluate.py").is_file()
        assert (root / "workspaces/evaluator/labels.csv").is_file()
        handoff = root / "workspaces/dataclean/DATACLEAN_HANDOFF.md"
        assert handoff.is_file() and handoff.read_text(encoding="utf-8").strip()
        assert (root / "workspaces/dataclean/clean.py").is_file()
        assert (root / "workspaces/eda/EDA_INDEX.md").is_file()
        assert (root / "workspaces/eda/EDA_HANDOFF.md").is_file()
        assert (root / "workspaces/eda/BASELINE_DESIGN.md").is_file()
        assert (root / "workspaces/eda/EDA_REPORT_00_OVERVIEW.md").is_file()

        best = runtime.tree.best_experiment_id()
        assert best is not None
        experiment = runtime.tree.get_experiment(best)
        assert experiment.eval is not None
        assert experiment.eval.primary == 1.0

        # dataclean 修复轮发生过：提交了两次才通过 gate
        assert runtime._client.dataclean_submits == 2
    finally:
        await runtime.aclose()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/integration/research/test_prepare_phase_e2e.py -v -m slow`

Expected: FAIL——PREPARE 尚未接入 dataclean 步骤 / 假客户端分发命中未知 agent，或断言失败。失败信息给出实际卡点后，按 Step 3 后的实现验证。

- [ ] **Step 3: 确认前置任务已到位**

确认 Task 4–7 已合并（`rt.init_mcp_tools` / `rt.agent_tools` / `rt.baseline_ideator_tools` / `run_dataclean_plan` / phase_runner 步骤 1b 都在）。若前置未完成，先完成它们再继续本任务。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/integration/research/test_prepare_phase_e2e.py -v -m slow`

Expected: PASS。再跑一遍 dataclean 契约 + 排序测试确认真实接线无回归：

Run: `uv run pytest test/integration/research/test_dataclean_agent_contract.py test/integration/research/test_prepare_phase_dataclean_order.py -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add test/integration/research/test_prepare_phase_e2e.py
git commit -m "test(e2e): full PREPARE phase walkthrough with dataclean repair round"
```

---

## 全量回归

实现全部 8 个任务后，运行：

Run: `uv run pytest test/unit/tools test/unit/research/test_runtime_pr7_tools.py test/unit/agent/test_prepare_dataclean_agent.py test/integration/research/test_dataclean_agent_contract.py test/integration/research/test_prepare_phase_dataclean_order.py -v`

Expected: 全部 PASS。

Run: `uv run pytest test/integration/research/test_prepare_phase_e2e.py -v -m slow`

Expected: PASS（E2E 完整链路）。

Run: `uv run pytest -m "not slow" -q`

Expected: 既有测试无回归（新增代码不改动任何既有行为路径；`run_prepare_phase` 只在步骤间插入新步骤）。

---

## Self-Review（对照 spec）

- **A1/A2（数据无关方法论 + gate）** → Task 5 prompt 全内容 + Task 6 `run_dataclean_plan` gate（只认 handoff 非空 + submit；不校验固定产物）。
- **A3（模块与命名）** → Task 5 `src/athena/agents/prepare_dataclean_agent.py` + `__all__`。
- **A4（phase_runner 接线，evaluator 后 EDA 前）** → Task 7 步骤 1b；EDA content 追加清洗说明；evaluator / baseline_ideator 的 `extra_tools` 替换。
- **A5（断点复用 = 文件存在）** → Task 7 步骤 1b 的文件存在判断。
- **B1（PR7 工具移植范围）** → Task 2（HF）+ Task 3（MCP），含裁剪（去 print、去重复 return、去 sandbox 打印）。
- **B2（依赖）** → Task 1。
- **B3（运行时装配）** → Task 4：`hf_tools` / `init_mcp_tools`（幂等 no-op + docstring 记 TODO）/ `agent_tools` / `baseline_ideator_tools` / `aclose` 关 managers / 空 `mcp_servers.json`。
- **B4（接线：只给 baseline_ideator 加 HF+MCP，SEARCH ideators 不变）** → Task 7（`rt.baseline_ideator_tools()` 用于 baseline；`ideator_tools()` 未动）。
- **C1（dataclean 单 agent 测试）** → Task 6：submit / 修复轮 / abandon / budget / 数据无关（COCO vs 表格）。
- **C2（PR7 工具测试移植）** → Task 2 / 3。
- **C3（E2E，Option 2 + 关 Kaggle + 本地合成数据 + agent-aware 假客户端）** → Task 8。
- **工程约束（代码规范 / 轻量 / 最小改动）** → 每个任务都标注了只改新行/小段；`register_prompt_agent` 未改。

无占位符：所有新代码都给出全文；移植任务用确定性的 `cp` + 明确裁剪指令（源码在仓库内，可查）。

类型一致性：`run_dataclean_plan` 在 Task 6 定义为 `-> str`，Task 7/8 按 `dataclean_handoff: str | None` 消费；`agent_tools` / `baseline_ideator_tools` / `init_mcp_tools` 在 Task 4 定义、Task 7/8 消费；`register_dataclean_agent` 签名在 Task 5 定义、Task 6/7 消费；E2E 的 worker 分发规则（无 user 内容）与 spec C3 表一致（worker 写 EDA_REPORT_00_OVERVIEW.md，与假 orchestrator 的 EDA_TODO.md 约定一致）。

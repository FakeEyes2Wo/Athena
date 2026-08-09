# Prompt 驱动的 Agents 改造设计

> 2026-08-09 · 分支 `feat/dynamic-eval`

## 背景

`agents/` 下多个业务 Agent 把「固定限制」写死在代码里：`data_agent.py` 的
`DEFAULT_ANALYSIS_SCRIPT`（约 140 行 EDA + 绘图 Python 模板）、`init_agent.py`
的 `_classify_task`/`_default_eval_script`（task understanding 的确定性判定与
eval.py 模板）、`report_agent.py` 的整类。目标是把这些固定限制改为 **prompt
限制**：LLM 按 prompt 输出内容，格式由 prompt 固定；生成逻辑不再写死在代码里。

方向已与用户确认：

1. **LLM 输出 md 文件，md 格式固定（由 prompt 规定）**。
2. data_agent 保留入口文件名 `analysis.py`，内容由 LLM 用通用工具写出并运行。
3. init_agent 输出 **task-understanding md 报告 + eval.py**；删除 EvalSpec 协议链。
4. report_agent.py 删除，report 改为 prompt 驱动 agent。
5. simple_agents.py 完善并改名 `builtin_agents.py`。
6. 无 model 直接报错，不保留确定性回退；测试用 `.env` 真实 DeepSeek API
   （`BASE_URL=https://api.deepseek.com`，模型 `deepseek-v4-flash`）。

## 目标架构

```
LLM(DeepSeek) ──ResponsesProvider──▶ Agent(ReAct) ──通用工具──▶ workspace
                                        │  system_prompt = prompts/*.md
                                        ▼
                        Python 编排器（data/init/report）→ 提交 Bundle / Artifact
```

业务 Agent 由两层组成：

- **内层**：`core.agent.runtime.Agent`（LLM ReAct 循环）+ `ToolRegistry`
  （通用工具）+ system prompt（从 `core/agent/prompts/*.md` 加载）。
- **外层**：Python 编排器（保留确定性编排：收集文件、提交 VersionedBundle、
  评审闭环、phase 推进）。**不允许**把脚本模板写死回外层。

## 改动清单

### 1. LLM 配置 — `core/agent/settings.py`（新）

- `load_dotenv()`（python-dotenv 已在 venv）。
- 读取环境变量：`DEEPSEEK_API_KEY`（已有）、`BASE_URL`（用户加入，默认
  `https://api.deepseek.com`）、`MODEL_NAME`（默认 `deepseek-v4-flash`）。
- `get_client()` → `AsyncOpenAI(api_key=..., base_url=...)`；`default_model()`。
- `provider.py` 的 `ResponsesProvider.client` 改为走 settings：优先注入的
  client，否则 `get_client()`（不再只认 `OPENAI_API_KEY`）。

### 2. 通用工具集 — `agents/tools/generic_tools.py`（新）

所有 prompt 驱动 agent 共用，沙箱限定 workspace 内（路径逃逸防护沿用
`WriteScriptTool` 的 `is_relative_to` 检查），复用 `LocalExperimentRuntime`/
`_run_command` 做沙箱执行：

- `read_file(path, start_line=None, end_line=None)` — 支持行范围（Claude Read
  语义）。
- `write_file(path, content)` — 创建/覆盖文件（Claude Write 语义）。
- `bash(command, timeout_s=120)` — 沙箱执行 shell，返回
  `{returncode, stdout, stderr}`（Codex Bash 语义）。
- `pwsh(command, timeout_s=120)` — PowerShell 变体（Windows）。
- 不加 `list_files`。Python 脚本经 `bash("python xxx.py")` 覆盖。

退役 `agents/tools/script_tools.py`（write_script/run_script/commit_result）。

### 3. Prompt 文件 — `core/agent/prompts/`

- `data_agent.md`（已有）— 接线；**补 EDA 报告固定格式小节**（如
  `## Task Overview` / `## Schema` / `## EDA` / `## Key Findings`）。
- `init_agent.md`（新）— task understanding：读数据集 → 输出固定格式
  task-understanding 报告（md）+ 写 `eval.py`。
- `report_agent.md`（新）— 综合已批准证据 → 固定格式最终报告（md）。
- `code_agent.md` / `plot_agent.md`（已有）— 接线；去掉对 EvalSpec 的引用。
- 每个 prompt 顶部列出可用工具（read_file/write_file/bash/pwsh）。

### 4. Agent 改造

**data_agent**：入口名保留 `ANALYSIS_ENTRYPOINT = "analysis.py"`。外层
`DataAgent.run`：构造内层 LLM agent（prompt=data_agent.md + 通用工具，cwd=
workspace）→ 运行 → 收集 `report.md` + `figures/*.png` → 提交 DataAnalysis
版本（保留 create/commit 所有权链）。删除 `DEFAULT_ANALYSIS_SCRIPT`。

**init_agent**：外层 `InitAgent.run`：内层 LLM agent（prompt=init_agent.md +
通用工具）→ 产出 task-understanding 报告 + 写 `eval.py` 到 workspace →
外层固化产物，返回 `{report_ref, eval_script}`。删除 `_classify_task`/
`_default_eval_script`。

**report**：删除 `report_agent.py`。report 类型改为内层 LLM agent
（prompt=report_agent.md + 通用工具 + evidence context_refs）→ 产出 report.md
Bundle。`run_report` 改指向。

**code / ideator / plot**：`builtin_agents.py` 中 CodeAgent/IdeatorAgent/
PlotAgent 支持 prompt 驱动（`code_agent.md`/`plot_agent.md`）；无 model 报错。

### 5. simple_agents.py → builtin_agents.py

改名，CodeAgent/IdeatorAgent/PlotAgent 保留并完善；`__init__.py`、`__init__`
导出、`project_runtime` import 同步更新。

### 6. 删 EvalSpec、留 eval.py

- `research/models.py`：删 `EvalSpec` / `EvalSpecChain` / `MetricDef`。
- `project_runtime.py`：删 `_eval_specs`/`eval_specs`/`freeze_eval_spec` 及
  持久化；新增 `_eval_ref`（eval.py artifact ref）作协议事实；`projected_phase()`
  的 `has_protocol` 改为 `self._eval_ref is not None`。
- eval.py 作为 init_agent 产物持久化（workspace + artifact ref），SEARCH 阶段
  的实验脚本依赖它。

### 7. model 接入 — `project_runtime.register_defaults`

签名改为 `register_defaults(*, model=None, client=None)`。model 为空时构造
LLM agent 即 `raise RuntimeError`（无回退）。有 model 时：
- data/init/report/code/plot/ideator 注册为 prompt 驱动 LLM agent
  （`create_agent_for` + `builtin_agents`/编排器）。
- supervisor/reflection 保持现有确定性实现（reflection 评审逻辑不变）。

### 8. 测试

- `tests/conftest.py`（新）：从 `.env` 建真实 client 的 fixture；
  `make_project(tmp_path)` → `register_defaults(model=...)`。
- LLM 相关测试标记 `@pytest.mark.slow`（pyproject 已有该 marker，
  `-m "not slow"` 可跳过）。
- 删 `test/unit/test_research_models.py`；`tests/test_core_model_contracts.py`
  移除 EvalSpec/MetricDef 导出断言；改 `test_project_runtime.py`（EvalSpec +
  report + register_defaults）与 `test_data_agent.py`（新 LLM 流程）。

## 不变量

- `analysis.py` 入口名保留；DataAnalysis 提交链（owner / parent_ref）不变。
- 评审闭环（Reflection → EvaluationPolicy）与六阶段 phase 投影不变。
- 无 model 时 LLM agent 构造/运行直接报错，绝不静默回退到确定性模板。
- prompt 是「限制」的唯一来源；不把脚本模板写回 Python 代码。

## 测试

- provider：base_url 注入（有/无 client）。
- generic_tools：read_file 行范围 / write_file 创建 / bash 沙箱 / 路径逃逸。
- data_agent：LLM 写 analysis.py → 提交 v1/v2；失败不提交。
- init_agent：产出 task-understanding 报告 + eval.py；projected_phase PREPARE。
- project_runtime：无 model 报错；report 阶段走 prompt 驱动。

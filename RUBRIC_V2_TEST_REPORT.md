# Rubric V2 测试报告

本报告只列出 Codex 本次确实执行过的命令。普通测试使用 fake/stub，不调用付费 API。为让临时 Git 仓库测试可提交，测试进程使用了会话级 `GIT_AUTHOR_*` / `GIT_COMMITTER_* = Athena Tests / athena-tests@example.invalid`，没有修改用户全局 Git 配置。

## 最终 Rubric targeted tests

Command:

```powershell
uv run --no-sync pytest -q test\unit\kaggle\test_supervisor_gate.py test\unit\research\rubrics test\unit\research\supervisor\test_ideator_wiring.py test\integration\research\test_task_seeding.py
```

Result:

```text
PASS — 48 passed in 3.37s
```

Notes:

覆盖两层 schema/逻辑、来源优先级、安全失败、minimize、Policy resume/freeze、批 ID/evidence、聚合、Selector preference/fallback、Gate 后接线和 task seeding。

## Research integration（排除原始收集故障）

Command:

```powershell
uv run --no-sync pytest -q test\integration\research --ignore=test\integration\research\test_search_recovery.py
```

Result:

```text
PASS — 64 passed, 1 warning in 91.02s
```

Notes:

warning 是 Windows asyncio subprocess transport 的 unraisable/closed-pipe 清理提示。

## Research integration 原始完整命令

Command:

```powershell
uv run --no-sync pytest -q test\integration\research
```

Result:

```text
FAIL — collection error
```

Notes:

`test/integration/research/test_search_recovery.py` 导入 `test.integration...`，但原始目录没有可导入 package，触发 `ModuleNotFoundError`。该文件与原始 ZIP SHA256 相同；不是 Rubric V2 回归。

## README broad suite 原始命令（修改版）

Command:

```powershell
uv run --no-sync pytest -q tests test\unit
```

Result:

```text
FAIL — collection error after 1 error
```

Notes:

`tests/test_gui_gateway_e2e.py` 导入 `test.unit._support` 时触发 `ModuleNotFoundError: No module named 'test.unit'`，测试在收集阶段中断。

## README broad suite 基线复现

Command（从原始 baseline 目录执行）:

```powershell
& '..\..\modified\Athena-main\.venv\Scripts\python.exe' -m pytest -q tests test\unit
```

Result:

```text
FAIL — same collection error after 1 error
```

Notes:

原始 ZIP 在相同依赖环境中复现同一 `test.unit` import 错误，证明不是本次修改引入。

## Broad suite（先排除收集故障，保留其余旧失败）

Command:

```powershell
uv run --no-sync pytest -q tests test\unit --ignore=tests\test_gui_gateway_e2e.py --deselect=tests\test_gui_gateway_transport.py::test_transport_resubscribes_after_project_switch --deselect=test\unit\test_agent.py::test_create_provider_routes_by_llm_provider_env --deselect=test\unit\test_agent.py::test_settings_provider_kind_default_and_validation
```

Result:

```text
FAIL — 3 failed, 1397 passed, 2 skipped, 2 warnings, 50 subtests passed
```

Notes:

在更新 provenance 兼容测试后，剩余三条失败是：

- GUI project switch fake factory 接收 1 个参数，但生产 handler 传 2 个。
- 两条 provider 测试允许/期待 `anthropic`，当前实现 allowlist 仅为 deepseek/openai/qwen。

对应 GUI/provider 源码和测试文件与原始 ZIP SHA256 相同；按 Scope 没有顺手修改。本次命令中的 Windows 反斜杠 node ID 没有成功 deselect 这三条测试，因此它们仍被执行并如实显示为失败；随后使用名称表达式正确排除。

## Broad suite（排除 4 个已验证旧问题）

Command:

```powershell
uv run --no-sync pytest -q tests test\unit --ignore=tests\test_gui_gateway_e2e.py -k "not transport_resubscribes_after_project_switch and not create_provider_routes_by_llm_provider_env and not settings_provider_kind_default_and_validation"
```

Result:

```text
PASS — 1397 passed, 2 skipped, 3 deselected, 2 warnings, 50 subtests passed in 106.66s
```

Notes:

两个 warning 均为 Windows asyncio closed pipe/event loop 清理提示；断言全部通过。

## Formatting

Command:

```powershell
& .\.venv\Scripts\python.exe -m black --check src\athena\agents\rubric_agent.py src\athena\agents\supervisor_agent.py src\athena\core\research_models.py src\athena\research\agent_turn_runner.py src\athena\research\phase_runner.py src\athena\research\rubrics src\athena\research\runtime.py src\athena\research\supervisor\prepare.py src\athena\research\supervisor\ranker.py src\athena\research\supervisor\state.py src\athena\research\supervisor\supervisor.py test\integration\research\test_task_seeding.py test\unit\research\rubrics test\unit\research\supervisor\test_ideator_wiring.py test\unit\kaggle\test_supervisor_gate.py
```

Result:

```text
PASS — 20 files would be left unchanged
```

## Repository style checker

Command:

```powershell
& .\.venv\Scripts\python.exe scripts\check_code_style.py src\athena\agents\rubric_agent.py src\athena\agents\supervisor_agent.py src\athena\core\research_models.py src\athena\research\agent_turn_runner.py src\athena\research\phase_runner.py src\athena\research\rubrics src\athena\research\runtime.py src\athena\research\supervisor\prepare.py src\athena\research\supervisor\ranker.py src\athena\research\supervisor\state.py src\athena\research\supervisor\supervisor.py
```

Result:

```text
PASS — exit code 0
```

Notes:

没有 R1/R2/R4/R5 阻断项。checker 报告了一批默认不阻断的 R3 docstring 提示，其中多数来自既有公开方法。

## Syntax compilation

Command:

```powershell
& .\.venv\Scripts\python.exe -m compileall -q src\athena\agents\rubric_agent.py src\athena\agents\supervisor_agent.py src\athena\core\research_models.py src\athena\research\agent_turn_runner.py src\athena\research\phase_runner.py src\athena\research\rubrics src\athena\research\runtime.py src\athena\research\supervisor\prepare.py src\athena\research\supervisor\ranker.py src\athena\research\supervisor\state.py src\athena\research\supervisor\supervisor.py
```

Result:

```text
PASS — exit code 0
```

## 真实 NLP 测试

Command:

```text
没有执行命令
```

Result:

```text
NOT RUN — Pending local NLP validation
```

Notes:

没有用户 API Key/真实运行授权，也未下载或运行 SMS Spam/BANKING77。测试步骤见 `RUBRIC_V2_NLP_TEST_GUIDE.md`。

## Ablation

Command:

```text
没有执行命令
```

Result:

```text
NOT RUN
```

Notes:

没有伪造 V1/V2 效果结论。可执行方案见 `RUBRIC_V2_ABLATION_GUIDE.md`。

## 2026-08-20 本地复核补充

以下为应用修改后的用户本机复核结果；本节补充而不覆盖上面的历史执行记录。

- Targeted Rubric/unit/integration：`48 passed in 3.46s`。
- Research integration（排除收集故障）：`63 passed, 1 failed, 1 warning`。失败为
  `test_turn_is_persisted_before_dispatch` 的 rolling-search 时序断言，单独重跑仍失败；
  已在 `main` 复现，不归因于 Rubric V2。
- Broad regression（排除已确认旧问题）：
  `1397 passed, 2 skipped, 3 deselected, 50 subtests passed`。
- Black、repository style checker 与 compileall：PASS。
- 官方 SMS Spam Collection 已下载并转换为 `5572 x 2` CSV；乳腺癌数据已准备为
  `569 x 31` CSV。DeepSeek API probe：PASS。
- 两次真实数据流程都在 SEARCH 前被 baseline Windows BOM/script-prepending 问题阻断；
  因而没有声称完整 PREPARE→SEARCH→VALIDATE 或实时 Layer 2 LLM review 已通过。
- 独立的确定性 Layer 2 smoke 直接调用本分支生产代码，验证 exact-ID guard、完整 review
  artifact 回读、透明聚合、`0.10` leakage penalty、Selector 排序与无伪造 AI 分数 fallback：
  PASS。对应正式 Hypothesis Rubric/ideator wiring 测试：`20 passed in 2.89s`。
- 按当前功能验收范围，V1/V2 ablation 未运行；不作效果优越性声明。

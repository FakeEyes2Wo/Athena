# Rubric V2 本地测试指南（Windows / PowerShell 零基础版）

## 需要安装什么

1. Python 3.11 或更高版本。
2. Git（部分 integration test 会创建临时 Git 仓库）。
3. uv（本项目的 Python 环境与命令运行器）。
4. Athena 源码。测试 fake/stub 不需要付费 API Key。

## 检查 Python、Git 和 uv

打开开始菜单，搜索 `PowerShell` 并打开。在蓝色/黑色窗口逐条输入：

```powershell
python --version
git --version
uv --version
```

Python 应显示 `3.11` 或更高。如果 `python` 找不到，可尝试 `py --version`。uv 找不到时可按 uv 官方安装说明安装，关闭并重新打开 PowerShell 后再检查。

## 进入 Athena 目录

在文件资源管理器打开 Athena 文件夹，点地址栏复制路径。PowerShell 输入（把占位符换成你的实际路径）：

```powershell
Set-Location -LiteralPath '<你的 Athena 路径>'
```

检查是否正确：

```powershell
Get-ChildItem
```

应看到 `pyproject.toml`、`src`、`test`、`tests`、`README.md`。

## 安装依赖

```powershell
uv sync
```

这会按 `pyproject.toml` 建立 `.venv` 并安装 dev dependencies。不要把 `.venv` 提交到 Git。

如果 `uv sync` 因没有 lock 文件而创建 `uv.lock`，先问团队是否希望提交该 lock；本次 Rubric V2 没有修改依赖，交付包不包含新生成的 `uv.lock`。

## Windows 临时 Git 身份

部分测试会在临时目录执行 `git commit`。如果电脑没有配置 Git 身份，可只在当前 PowerShell 窗口设置环境变量，不修改全局账号：

```powershell
$env:GIT_AUTHOR_NAME='Athena Tests'
$env:GIT_AUTHOR_EMAIL='athena-tests@example.invalid'
$env:GIT_COMMITTER_NAME='Athena Tests'
$env:GIT_COMMITTER_EMAIL='athena-tests@example.invalid'
```

窗口关闭后这些值自动消失。

## 1. Targeted Rubric V2 tests

先跑最相关、最快的一组：

```powershell
uv run pytest -q test/unit/kaggle/test_supervisor_gate.py test/unit/research/rubrics test/unit/research/supervisor/test_ideator_wiring.py test/integration/research/test_task_seeding.py
```

本次交付环境的期望结果是：

```text
48 passed
```

## 2. Research integration tests

仓库原始 `test/integration/research/test_search_recovery.py` 当前有 package import 收集问题；在团队修复它之前，先运行其余 research integration：

```powershell
uv run pytest -q test/integration/research --ignore=test/integration/research/test_search_recovery.py
```

本次交付环境结果为 `64 passed`。不要因为这个 workaround 就把被忽略的文件写成 Passed；它仍是已知旧问题。

## 3. Broader tests

README 推荐：

```powershell
uv run pytest -q tests test/unit
```

原始仓库会在 `tests/test_gui_gateway_e2e.py` 收集时因 `ModuleNotFoundError: test.unit` 中断。本次基线与修改版都能复现。

要验证其余 broad suite，可使用：

```powershell
uv run pytest -q tests test/unit --ignore=tests/test_gui_gateway_e2e.py -k "not transport_resubscribes_after_project_switch and not create_provider_routes_by_llm_provider_env and not settings_provider_kind_default_and_validation"
```

本次交付环境结果为：

```text
1397 passed, 2 skipped, 3 deselected, 50 subtests passed
```

三个 deselected 旧失败分别是 GUI runtime factory 参数数量不匹配，以及两条 provider allowlist 旧测试与现有实现不一致；相关源码/测试文件与原始 ZIP 哈希一致，未被 Rubric V2 修改。

## 4. 格式、代码规则和语法

```powershell
uv run black --check src/athena/agents/rubric_agent.py src/athena/agents/supervisor_agent.py src/athena/core/research_models.py src/athena/research/agent_turn_runner.py src/athena/research/phase_runner.py src/athena/research/rubrics src/athena/research/runtime.py src/athena/research/supervisor/prepare.py src/athena/research/supervisor/ranker.py src/athena/research/supervisor/state.py src/athena/research/supervisor/supervisor.py
uv run python scripts/check_code_style.py src/athena/agents/rubric_agent.py src/athena/agents/supervisor_agent.py src/athena/core/research_models.py src/athena/research/agent_turn_runner.py src/athena/research/phase_runner.py src/athena/research/rubrics src/athena/research/runtime.py src/athena/research/supervisor/prepare.py src/athena/research/supervisor/ranker.py src/athena/research/supervisor/state.py src/athena/research/supervisor/supervisor.py
uv run python -m compileall -q src/athena/research/rubrics src/athena/agents/rubric_agent.py src/athena/research/agent_turn_runner.py src/athena/research/runtime.py
```

仓库 style checker 的 R3 docstring 输出默认是提示；R1/R2/R4/R5 或非零退出码才阻断。

## PASS、FAIL、ERROR、BLOCKED 是什么

- PASS：命令退出码为 0，结尾显示 passed，且没有隐藏失败。
- FAIL：测试确实执行，但断言不满足或代码抛异常。
- ERROR：常见于测试收集、fixture、import 或环境初始化阶段失败，同样不是通过。
- BLOCKED：环境/权限/依赖使测试根本无法开始。
- SKIPPED：测试被条件跳过，不等于验证过该行为。
- NOT RUN：没有执行；例如本次真实 NLP 和 Ablation。

## 常见问题

### `ModuleNotFoundError` 怎么办

先看缺的是第三方包还是仓库模块：

1. 第三方包：重新运行 `uv sync`，确认命令是 `uv run pytest ...`。
2. `test.unit`：这是当前原始仓库已知 package/import 问题。不要为完成 Rubric V2 擅自大改测试布局；按上面的 ignore 命令继续验证其余套件，并在 PR 如实报告。
3. 自己新增模块找不到：确认文件路径、拼写、`__init__.py` 和当前目录。

### API Key 缺失怎么办

普通 unit/integration tests 使用 fake/stub，不需要 Key。真实 NLP/LLM 运行才需要：

1. 复制 `.env.example` 为 `.env`。
2. 填团队允许的 provider Key。
3. `.env` 已被 Git 忽略，但仍要在 Commit 前确认没有选中它。
4. 不要把 Key 粘贴到 issue、PR、截图或日志。

### 网络问题怎么办

- `uv sync` 超时：检查代理、防火墙、DNS，稍后重试；不要通过删除依赖来“修好”。
- 模型调用超时：先确认 provider/base URL/Key 和账户额度，再查看 Athena agent 日志。
- 数据下载失败：用浏览器手动从官方来源下载，再按 NLP 指南放到本地目录。

### 什么错误先不要乱改代码

- Git `Author identity unknown`：设置上述临时环境变量，不需要改 Athena。
- `test.unit` import 收集错误：已在原始 ZIP 复现，先记录，不属于 Rubric 改动。
- provider 测试期望 `anthropic`，实现只允许 deepseek/openai/qwen：是存量测试/实现漂移，Rubric V2 不应顺手扩 provider。
- Windows `PytestUnraisableExceptionWarning` 的 closed pipe/event loop 提示：本次通过套件里出现过，先记录；若测试仍为 passed，不要把它误当 Rubric 断言失败。
- 缺 API Key 的真实 LLM 测试：标为 BLOCKED/NOT RUN，不要在代码中写死假 Key 或伪造结果。

## 建议执行顺序

```text
uv sync
  -> targeted 48
  -> research integration 64
  -> broad suite（记录旧例外）
  -> style/compile
  -> 检查 git diff
  -> Commit
```

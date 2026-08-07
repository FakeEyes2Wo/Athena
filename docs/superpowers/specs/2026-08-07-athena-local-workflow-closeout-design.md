# Athena 本地可信工作流收口设计（含 Titanic 试跑）

**Status:** Approved
**Date:** 2026-08-07
**Scope:** `src/main.py`, `src/athena/code/`, `src/athena/git_workspace.py`,
`src/athena/workflows/`, `src/athena/workflows/prepare/`,
`src/athena/workflows/validate/`, `tests/`, `test/unit/`, `README.md`,
`docs/README.md`, `examples/titanic/`

## Goal

按已批准的《Athena Trusted Experiment Execution》计划（任务 2–6），收口
`src/main.py` 的本地可信执行路径，使完整 PREPARE → SEARCH → VALIDATE → REPORT
工作流在离线测试与 `examples/titanic` 真实数据上均可跑通。本次明确**不做
Docker**：只完成本地执行运行时、生成树策略与修复循环、完整 Git diff 与耐久证据、
冻结 final-test、CLI 预检与本地模式语义。

## 背景与现状

- 任务 1（phase-scoped prepared data + trusted evaluation）已完成：
  `EvaluationInputs`、`TrustedEvaluator`、`PreparedWorkflowData` 不含
  `split_manifest_path`。
- `src/main.py` 因引用已删除的 `split_manifest_path` 而破坏，离线 E2E 失败。
- 工作区存在未提交的针对性修复（runner 使用 `sys.executable`、Git ignore 处理、
  SEARCH 提示词区分 validation/test），须并入对应任务而非回退。
- `examples/titanic/` 已加入（未跟踪）：`train.csv`、`test.csv`、
  `gender_submission.csv` 与 `.complete/competitions/titanic/bundle.complete`。

## 设计

### 1. 实验执行运行时（本地）

`src/athena/code/execution.py` 新增：

- `ExecutionRequest(entrypoint, cwd, timeout_s, environment, readonly_inputs)`。
- `ExperimentRuntime.run(request) -> ExecutionOutput` 与
  `ExperimentRuntime.preflight() -> None`。
- `LocalExperimentRuntime(base_environment=None)`：以 `sys.executable` 运行
  `entrypoint`，环境仅允许 `PATH`、`SYSTEMROOT`、`WINDIR`、`TEMP`、`TMP` 及显式
  `ATHENA_*` 变量，禁止 `.env` 值与模型凭据；固定 cwd、超时、捕获 stdout/stderr。

`run_script` 改为对 `LocalExperimentRuntime` 的兼容包装，保留活跃解释器修复。

### 2. 生成树策略与修复循环

`src/athena/code/file_policy.py` 新增 `GeneratedTreePolicy.validate(root, changed_paths)`：

- 允许 worktree 内任意常规辅助文件（新增/修改/删除）。
- 保护 `.git/`、`.gitignore`、`eval.py`、`eval_spec.json`、
  `.athena/phase_manifest.json` 及运行时输出路径。
- 拒绝指向 worktree 之外的符号链接；最终必须存在 `run_experiment.py`。

`GenerationResult` 增加 `files_deleted`；后端在快照中报告删除路径。新增
`render_backend_prompt(prompt, previous_outputs, history)`：每轮至多保留三轮历史与
各 4000 字符 stdout/stderr，修复轮携带先前执行与评估诊断。

`CodeAgent.execute(..., worktree, inputs: EvaluationInputs)` 改由 `CodeAgent`
持有循环：调用后端 → 树策略校验 → 注入的运行时执行一次 → `TrustedEvaluator`
评估 → 失败则回填有界诊断并重试至多 `max_rounds`。`CodeEngine` 保留为通用兼容引擎。

### 3. 完整 Git diff 与耐久证据

- `GitWorkspace.diff()` 返回结构化 `GitDiff(ref, paths)`，paths 含新增/修改/删除，
  排除 `.gitignore` 匹配的运行时输出。
- `commit()` 保留审查后不得变更的校验（已实现）。
- diff、logs、evaluation、predictions 均以 `sha256:` 内容寻址引用写入
  `LocalArtifactStore`，worktree 移除后仍可读。

### 4. 冻结 final-test

- `CodeAgent.execute_frozen(..., worktree, inputs=test_inputs)`：不调用任何后端，
  无重试、无修复；仅以 final-test 阶段清单执行已提交 SOTA 树一次，可信评估 test
  标签，返回与 SOTA 完全相同的 commit。
- baseline 与 `SearchLoop` 注入 `prepared.validation_inputs`；延迟 Validator 同时
  持有 validation 与 test 两套输入；ablation 用 validation，final-test 仅用 test。

### 5. CLI 预检与本地模式

- `RunConfig` 增加 `execution: Literal["local"] = "local"`；摘要增加
  `execution` 与 `strong_isolation: false`。
- 预检（在创建输出目录前）：CSV 头含 target、按 0.2/0.2 比例至少 5 个可用 target
  行、task/metric/direction 组合受 `EvaluatorFactory` 支持。
- 本地模式向 stderr 输出一次隔离性警告。退出码保持 2 / 1 / 130。

### 6. Titanic 示例与验收

- 重写离线 E2E：假后端可生成 `run_experiment.py` 与辅助 `model.py`，读取
  `.athena/phase_manifest.json`，写行 ID 键控预测（不含标签）；断言 4 个成功实验、
  `sha256:` diff/log/evaluation/prediction 引用、final-test 不调用后端且 commit
  不变、SEARCH 提示词不含 test 路径、worktree 清理后证据可读。
- 真实试跑：
  `uv run python src/main.py --data examples/titanic/train.csv --target Survived
  --model <ideator model> --backend codex --output-dir .athena/titanic-run`。
- 将未提交的 runner / git ignore / 提示词 / E2E 修复并入对应任务。

## 错误处理

- 配置错误在开展 agent 工作前返回退出码 2。
- 工作流/后端/评估/Git 失败返回 1；中断返回 130。
- 每个阶段边界与顶层失败路径保存最新 ResearchTree。

## 测试

- 每个任务 RED → GREEN。
- 最终门禁：`uv run pytest -q tests test/unit`；对改动的 Python 文件
  `black --check`；`git diff --check`；`uv run python src/main.py --help` 退出 0；
  无效配置退出 2 且无 traceback。
- 真实 Titanic 试跑成功，产物齐备。

## 明确不做（YAGNI）

- 不做 Docker 执行运行时与镜像。
- 不做分布式执行、Elo 策略、Kaggle MCP 接入等计划外能力。

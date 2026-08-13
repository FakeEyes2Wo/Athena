# Eval 合同泛化设计：predictions 目录 + evaluator handoff spec

Status: draft
Date: 2026-08-13

## 背景与动机

Athena 面向 AI4ML/AI4S 自动研究，评估阶段（PREPARE 基线 / SEARCH 打分 / VALIDATE
复现）必须适配「一切机器学习任务」，而不仅是泰坦尼克这类表格二分类。当前 eval
合同对预测产物做了三个硬编码假设，遇到回归、多分类、分割、生成等任务就会失效：

1. **单文件假设**——`experiment.json` 的 `outputs.predictions` 是一个字符串路径，
   只指向单个文件；无法表达「一次实验产出多个预测文件」（如每张图一个 mask、每个
   样本一个 embedding）。
2. **纯文本假设**——预测注入走 `write_text`，二进制产物（图片 / 音频 / 权重）会挂。
3. **语义分散**——eval 的「设置格式」与「判定标准」散落在 `metric.json`、`eval_script`
   源码、`RESEARCH_HANDOFF.md` 三处，没有一个权威、可读的单一来源。

## 目标形态

- `predictions` 产物是**一个目录**（多文件、二进制安全），不是单个文件。
- `evaluator/` 目录**自包含**三件套：spec（handoff 文档）+ impl（`evaluate.py`）+
  真值（`labels/`）。
- handoff 文档是**唯一权威的 eval 规格**，**约束最终产物**——定义 `predictions/`
  目录里该有什么、每个文件的格式、以及判定标准；`evaluate.py` 是它的可执行实现，
  SEARCH 的 ideator/worker 读它来理解「要产出符合什么约束的 predictions 目录」。
- 系统保持**最薄合同**：只认 `metric.json` 的 `eval_script` + 脚本 stdout 的
  `{"primary": <float>}`，不解析 handoff 文档。

## 目录结构

```
workspace/
  experiment.json          # commands + outputs.predictions(→目录) + outputs.report
  metric.json              # {"eval_script": "evaluator/evaluate.py"}
  evaluator/
    HANDOFF.md             # 约束最终产物：predictions 的「设置格式 + 判定标准」
    evaluate.py            # 读 predictions/ + labels/，按 HANDOFF 标准算分
    labels/                # ground truth（任意格式，目录或单文件）
    pyproject.toml
  predictions/             # 目录，多文件，任意格式（受 HANDOFF.md 约束）
  REPORT.md
```

## 语义

- **HANDOFF.md（spec）**：Markdown，Agent 可读。描述（a）`predictions/` 目录的布局与
  每个文件的格式（设置格式），（b）metric 如何计算、什么判定为对/错（判定标准）。
  它是「最终产物」的约束来源，不是给系统解析的机器配置。
- **evaluate.py（impl）**：agent 写的 Python，实现 HANDOFF.md 的约束。它以 bundle 根
  为 cwd，读 `predictions/` 目录与 `labels/`，格式不符时非零退出（视为 scoring 失败），
  成功时打印一行 `{"primary": <float>}` 到 stdout。
- **metric.json（最薄合同）**：只声明 `eval_script` 路径，保持不变。
- **系统（不解析 spec）**：冻结 `evaluator/`（含 HANDOFF.md + labels）、注入
  `predictions/` 目录、跑 `evaluate.py`、读 stdout 拿 `primary`。任务类型差异全部由
  agent 自描述，系统不关心回归/多分类/分割/生成。

## 系统侧改动

1. **`outputs.predictions` 目录化**：`run_turn`（SEARCH）与 `_execute_predictions`
   （VALIDATE）不再读单个 `predictions.csv`，而是把 `predictions/` 目录整树打包
   （相对路径 → 内容 ref）。`ExperimentManifest.outputs` 的 `predictions` 值仍为字符串
   （目录相对路径），但语义从「文件」升级为「目录」。
2. **目录注入 + bytes**：`DataScriptRunner.run` 的 `extra_files` 从
   `dict[相对路径 → 文本]` 升级为支持整目录树 + 字节内容（沿用冻结 `tree` 已有的
   `put_bytes`/`get_bytes` 机制），二进制预测不再经 `write_text`。
3. **HANDOFF.md 进 bundle**：`_freeze_evaluator` 冻结 `evaluator/` 目录树时天然包含
   `HANDOFF.md`（`freeze` 已 `rglob("*")`），无需特殊处理；但 SEARCH 的 ideator/worker
   需要能读到它——由 SEARCH plan 的上下文（context_ref / tree_ref）把 HANDOFF.md 内容
   透传给 propose/hypothesis 阶段。

## 数据流

- **PREPARE**：agent 产出 `evaluator/`（含 HANDOFF.md）+ `predictions/` + `metric.json`
  + `experiment.json`。`_freeze_evaluator` 冻结 `evaluator/`；`_prepare_metric` 以
  workspace 为 cwd 跑 `eval_script`，读 stdout 的 `primary`。
- **SEARCH**：`run_turn` 跑 manifest 命令产出 `predictions/` 目录，整树打包后经
  `TrustedEvaluator.score`（`predictions_path` 指向目录）注入冻结 bundle，跑
  `evaluate.py` 打分。
- **VALIDATE**：`_execute_predictions` 复现 `predictions/` 目录，`_score_result` 用冻结
  评估器独立打分，计算 generalization gap。

## 错误处理

- `evaluate.py` 因格式不符/缺文件非零退出 → `DataScriptRunner.run` 抛
  `CalledProcessError` → `TrustedEvaluator.score` 包成 `ValueError` → 上层
  `scoring_failed`（同 Plan 修复，不误判成基础设施故障）。
- 无 `{"primary": float}` stdout → `_read_output` 抛错 → 同上。
- 目录缺失/空 → `run_turn`/`_execute_predictions` 抛「predictions output missing」。

## 测试

- 单测：`DataScriptRunner.run` 注入目录树 + 二进制文件；`_read_output` 已覆盖 stdout
  合同；manifest `outputs.predictions` 指向目录。
- 集成：`test_prepare_agent_contract` 用目录预测 + HANDOFF.md 走完 PREPARE→SEARCH 打分。
- 端到端：真实 LLM 跑一个目录预测的 Titanic（`predictions/predictions.csv`），验证
  PREPARE→SEARCH→VALIDATE→COMPLETED。

## 迁移

- 现有 Titanic 例子的 `predictions.csv`（单文件）改为 `predictions/predictions.csv`
  （目录），`evaluate.py` 读 `predictions/predictions.csv`，HANDOFF.md 写明该格式。
- 现有单测 fixture 中 `outputs.predictions` 为单文件的用例，改用目录路径。
- `predictions_path` 单文件线程（已提交 `b240a06`）作为过渡形态保留，本设计落地后
  由目录注入取代。

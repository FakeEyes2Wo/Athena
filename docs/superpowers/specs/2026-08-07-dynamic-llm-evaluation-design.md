# 动态 LLM 评估设计

Status: draft
Date: 2026-08-07
Scope: `src/athena/evaluation/`, `src/athena/workflows/prepare/`, `src/athena/workflows/search/code_agent.py`

## 背景与问题

实验的评估指标不固定：有的任务用标准指标（f1_macro / rmse），有的用混合指标（如 `0.7*roc_auc + 0.3*f1_macro`），有的用自定义打分规则（Kaggle 类竞赛）。现有实现的三个缺陷：

1. **指标靠关键词猜**：`_parse_intent` / `EvaluatorFactory` 从 "regress"→rmse、"auc"→roc_auc 这种字符串匹配选指标，不懂任务语义，混合/自定义指标无处表达。
2. **eval.py 写死 f1**：`CodeAgent._write_evaluator` 每次实验写死 `f1_score(macro)`，完全没读 `EvalSpec`。
3. **任务描述未进入元数据**：`TaskMetaData` 没有自然语言描述字段（Kaggle 的 `CompetitionInfo.description` 有但没接）。

## 方案概览

现有架构骨架是正确的（冻结 `EvalSpec`、worktree 内跑 eval.py、`Comparator` 比主标量），保留不动。只改三处：

- **PREPARE 时 LLM 规划器一次调用**，根据任务描述 + 数据画像产出冻结的 `EvalSpec` 和一份 `eval.py` 源码。
- **eval.py 从规约生成、随 baseline 提交、全程共用**（所有候选实验跑同一份，保可比较）。
- **`EvalSpec` / `EvalResult` 增加最小字段**（`protocol`、`fold_scores`），支持 holdout 与 CV-5 两种评估协议。

核心权衡：**指标对比较层保持不透明** —— 它只需要标量 / 折值，怎么算交给"冻结的 eval.py"负责。混合/自定义指标靠描述驱动代码生成自然支持，代码提交进仓库、可复现。

## 设计

### 1. 数据模型（`evaluation/types.py`）

`MetricDef` 字段不变：`{name, direction, description}`。`description` 成为真正的"指标规约"（混合指标写进描述，如 `"0.7*roc_auc + 0.3*f1_macro"`）。

```python
class EvalProtocol(BaseModel):
    method: Literal["holdout", "cv"]
    n_folds: int = 5                      # cv 专用

class EvalSpec(BaseModel):                # 仍 frozen
    primary: MetricDef
    secondary: list[MetricDef] = Field(default_factory=list)
    protocol: EvalProtocol = Field(default_factory=lambda: EvalProtocol(method="holdout"))
    eval_script: str = ""                 # 冻结的评估实现（planner 生成或默认模板）
    split_seed: int = 42
    test_ratio: float = 0.2

class EvalResult(BaseModel):
    experiment_id: str
    primary: float
    secondary: dict[str, float] = Field(default_factory=dict)
    fold_scores: list[float] = Field(default_factory=list)   # 新增：cv 每折主指标值
    per_sample: ArtifactRef              # 保留：诊断用途，不参与显著性
```

> 机制修正：当前 `LocalGitWorkspace.diff()/commit()` 未被 SEARCH 接线，工作区内容从不提交、候选不继承 baseline 文件。因此 `eval_script` 内嵌进冻结的 `EvalSpec`，`CodeAgent` 在每个 worktree 里从 `spec.eval_script` 写入**字节级相同**的 eval.py —— 不依赖分支继承，同时保证所有实验运行同一份评估代码。

`TaskMetaData`（`research/models.py`）增加 `description: str = ""`，由 `_configure` / `_parse_intent` 捕获用户消息，或来自 Kaggle 描述。

### 2. MetricPlanner（`workflows/prepare/metric_planner.py`，替代 `EvaluatorFactory`）

- 输入：`task.description`、`task.constraints`、`DataProfile`。
- 硬约束规则：若 `task.primary_metric` 与 task_type 默认猜测不同（说明用户显式指定了指标），则作为硬约束传给 planner；否则 planner 自由选择。
- 输出（schema 强制，pydantic）：

```python
class MetricPlan(BaseModel):
    spec: EvalSpec          # 冻结的评估协议（内含 eval_script）
    rationale: str          # 为什么选这个指标（审计）
```

- 一次 LLM 调用，不可用时**回退到现有 `_DEFAULT_METRICS` 按 task_type 兜底**（`EvaluatorFactory` 的逻辑保留为 `default_spec(task_type)`），eval_script 用 bundled 默认模板（按默认指标生成）。
- planner 依据协议生成 `spec.eval_script`：holdout → 读 predictions.csv + labels.csv 出标量；cv → 额外读 fold_ids.csv 出每折值。`spec` 冻结后 `eval_script` 随之冻结。

### 3. eval.py 生命周期（`workflows/search/code_agent.py`）

- **PREPARE**：planner 产出 `spec.eval_script` → **smoke 校验**（合成数据跑一遍，验证不崩溃、输出符合 schema）→ 冻结进 `EvalSpec`。
- **SEARCH**：`CodeAgent._write_evaluator` 改为 `_ensure_evaluator`：每个 worktree 从 `spec.eval_script`（为空时用 `default_eval_script`）写 eval.py，已有则不覆盖。所有实验运行字节级相同的评估代码。
- **契约**：读 `predictions.csv`（y_pred）+ `labels.csv`（y_true），cv 时读 `fold_ids.csv`；写 `eval_result.json`：`{experiment_id, primary, secondary?, fold_scores?}`。Athena 侧校验 experiment_id 匹配（来自 `ATHENA_EXPERIMENT_ID` 环境变量，因为 eval.py 全实验共用、不能内嵌单次实验 ID）、primary 有限、cv 时 `len(fold_scores) == n_folds`。

### 4. 评估协议

- **holdout**：实验（run_experiment.py）预测最终测试集 → eval.py 出单个标量。Kaggle 排行榜风格。
- **cv**：实验在 train 上做 5 折 OOF 预测，写 `fold_ids.csv` → eval.py 每折出一个标量。比较器在折值上比。

协议是冻结 `EvalSpec` 的一部分 → baseline 与候选共用同一协议 → 可比。

### 5. 比较器（`evaluation/comparator.py`）

- 两个 `EvalResult` 均有非空 `fold_scores` 且等长 → 在折值上做配对检验（`ttest_rel`）。
- 否则 → 现有 per-sample 路径（不变）。

### 6. 集成点

| 位置 | 改动 |
|---|---|
| `research/runtime.py` | `_parse_intent`/`_configure` 捕获 `description` |
| `workflows/prepare/evaluator_factory.py` | 变为 `MetricPlanner`（async）+ `default_spec` 兜底 |
| `workflows/prepare/baseline.py` | 不改（eval.py 由 code_agent 从 `spec.eval_script` 写入） |
| `workflows/search/code_agent.py` | `_write_evaluator` → `_ensure_evaluator`；env 传 `ATHENA_EXPERIMENT_ID`；契约校验含 fold_scores |
| `evaluation/comparator.py` | fold_scores 折级比较分支 |
| `examples/trial_run.py`、`examples/ai4ml_pipeline.py` | 改用 `MetricPlanner.plan` |

## 测试

- **planner**：schema 强制输出；LLM 失败时回退默认指标。
- **eval_script smoke**：合成数据上运行校验输出契约。
- **comparator**：折级比较 vs per-sample 比较两条路径。
- **端到端**：混合指标（如 `0.7*roc_auc + 0.3*f1_macro`）从规划 → eval.py → 比较 全链路。

## 明确不做（YAGNI）

- 不做 LLM-as-judge（开放式无 ground-truth 任务）——后续版本。
- 不做 Athena 侧指标注册表 —— 计算全权交给冻结的 eval.py。
- 不做搜索中途动态改指标 —— 冻结保证统计有效性。

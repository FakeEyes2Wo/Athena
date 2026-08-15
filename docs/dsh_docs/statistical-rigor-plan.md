# 统计严谨性升级 — 实施计划（paired bootstrap + Bayesian 显著性）

Status: 待实施（active）
Owner: Athena maintainers
Last verified: 2026-08-11
Scope: SOTA 比较的统计严谨性——引入 paired bootstrap（置信区间 + p-value），真实产出 `ComparisonVerdict.p_value`，并作为 search-selection 计划任务 D 的 Bradley-Terry strength 输入；Bayesian 显著性（P(improvement)）为可选增强。
Source of truth: `src/athena/research/evaluation.py`、`src/athena/research/contracts.py`、
`src/athena/core/research_models.py`、`src/athena/research/supervisor/{supervisor,experiment}.py`、
`src/athena/core/agent/prompts/code_agent.md`、`docs/supervisor_design.md`
关联计划：`docs/dsh_docs/search-selection-upgrade-plan.md`（任务 B tie tolerance、任务 D BT strength）

## 1. 目标与成功标准

在「不破坏信任边界」的前提下，为 SEARCH 的 SOTA 比较引入统计证据：

1. **paired bootstrap**：对候选与参考实验的 per-sample 分数差做配对重采样，产出 mean-diff 的置信区间与显著性 p-value。
2. **`ComparisonVerdict` 被真实生产**：`supervisor.py` 里 `complete_experiment(..., verdict=None)` 不再恒为 `None`，改为由 Comparator 依据 CI 产出 `winner ∈ {baseline, candidate, tie}` + `p_value`。
3. **喂给 BT strength**：`ComparisonVerdict.p_value`（或 `winner`）作为 search-selection 计划任务 D 的 Bradley-Terry 强度信号，替换/增强当前的确定性 `WIN/DRAW/LOSS`（交叉引用）。
4. **Bayesian 显著性（可选增强）**：追加 `P(improvement) = P(mean_diff > 0)` 作为连续风险信号。

**信任边界（成功标准的硬约束）**：平台（Comparator / Supervisor）**绝不读取 test/final labels，也不重实现任何指标公式**。per-sample 分数只能由 trusted evaluator（唯一读 labels 的一方）产出；平台只对 trusted evaluator 已产出的 per-sample 分数做纯统计重采样。任何违反该边界的实现都视为失败。

**可回退**：per-sample 分数缺失时，确定性回退到现有单点 `test_score` 比较（无 bootstrap、`verdict=None` 或退化 `winner`），不静默伪造 CI/p-value。

## 2. 背景：当前实现的精确状态（含证据）

| # | 现状 | 证据 | 问题 |
|---|---|---|---|
| A | `TrustedEvaluator.score` 只返回单一 `test_score`；`CandidateEvaluation` 无 per-sample 字段 | `evaluation.py:25-75`（返回体 `:71-75`，`output_schema={"primary": None}` 在 `:52`）；`contracts.py:36-43` | 平台无 per-sample 分数可做 paired 重采样 |
| B | `EvalResult.per_sample` 是 `ArtifactRef`，但被塞入 `evidence_ref`（JSON 元数据），非 per-sample 分数 | `research_models.py:68-75`；`supervisor.py:744-754`（`per_sample=evidence_ref` 在 `:749`） | 字段语义错位，`per_sample` 未承载可重采样数据 |
| C | `ComparisonVerdict(winner, p_value)` 已定义但**无人生产** | `research_models.py:77-81`；`supervisor.py:751` `verdict=None`；全库 grep 无 `p_value` 生产者 | BT 的"真实两两比较"数据缺失 |
| D | 预测产物契约 `predictions.csv` 每行 `__athena_row_id,prediction` | `code_agent.md:20` | 行身份键已存在，可供 per-sample 配对对齐 |
| E | 设计基线显式 `TODO(search-bootstrap)` | `supervisor_design.md:287-291`（TODO 文本 `:290`） | 明确要求「test predictions 重采样 + EvalSpec 获得 seed/count/CI/min_effect 合同后再加」 |
| F | 设计基线 tie tolerance 为 `math.isclose(rel=1e-9, abs=1e-12)` | `supervisor_design.md:269-273`（`math.isclose` 在 `:272`） | 与本计划显著性阈值的关系须显式决策（见 §8）；与 search-selection 任务 B 一致 |

## 3. 阶段一：扩展 eval 契约，让 trusted evaluator 产出 per-sample 分数（T1）

> 目的：让平台拥有「可重采样」的 per-sample 分数，且该分数**仍由 trusted evaluator 产出**（它读 labels），平台不接触 labels。

### 3.1 改动文件

- **`src/athena/research/contracts.py:36-43`**（`CandidateEvaluation`）：新增 `per_sample_ref: ArtifactRef | None = None`——指向按 `__athena_row_id` 键的 per-sample 分数 artifact（JSON/CSV，每行 `row_id, score`）；`score` 为「该行对 primary 的贡献」，须满足 **`aggregate(per_sample) == test_score`**（如 mean），这是 bootstrap 均值可比的契约前提。
- **`src/athena/research/evaluation.py:25-75`**（`TrustedEvaluator.score`）：
  - `output_schema={"primary": None}`（`:52`）扩展为可请求可选 per-sample 输出（如 `{"primary": None, "per_sample": None}`）。
  - 解析并校验 per-sample 输出：非空、行数一致、`aggregate ≈ primary`（在 `math.isclose` 内）；通过则 `per_sample_ref` 落盘并回填，否则 `per_sample_ref=None`（**确定性回退，不伪造**）。
  - 校验失败不升级为评分失败——per-sample 是可选证据，primary 仍是权威分数。
- **eval 脚本契约**（`init_agent.md` 产出的 `eval.py`，与 `code_agent.md` 的 `predictions.csv` 对齐）：新增「可选 per-sample 分数输出」约定——eval 脚本除 `primary` 外可声明一个 per-sample 分数文件，行格式 `__athena_row_id,score`，与 `predictions.csv` 行身份一致。
- **`src/athena/research/supervisor/supervisor.py:744-754`**：`EvalResult.per_sample` 改填 trusted evaluator 产出的 `per_sample_ref`（当前误填 `evidence_ref`）；`evidence_ref` 仍保留在 `artifacts["evidence"]`。

### 3.2 测试

- `test/unit/research/test_evaluation.py`：`score` 有 per-sample（回填 ref + 校验通过）、无 per-sample（`per_sample_ref=None` 回退）、`aggregate ≠ primary`（回退不抛错）。
- 契约测试（`tests/test_core_model_contracts.py` 或 `tests/test_core_public_api.py`）：`CandidateEvaluation.per_sample_ref` 字段存在且可选。
- `test/unit/research/supervisor/test_supervisor.py`：`EvalResult.per_sample` 填 `per_sample_ref` 而非 `evidence_ref`。

### 3.3 验收

```bash
uv run pytest test/unit/research/test_evaluation.py \
  test/unit/research/supervisor/test_supervisor.py \
  tests/test_core_model_contracts.py -q
```

## 4. 阶段二：Comparator 产出 `ComparisonVerdict(p_value)` 并接线（T2）

> 目的：平台侧做 paired bootstrap 纯统计，真实产出 `ComparisonVerdict`，供 SOTA 比较记录与 BT strength 消费。

### 4.1 新组件 `Comparator`（`src/athena/research/supervisor/comparator.py`，新文件）

- **输入**：`candidate_per_sample`、`reference_per_sample`（两列 per-sample 分数，按 `__athena_row_id` 对齐）、`direction`、`seed`、`n_bootstrap`、`ci_level`。
- **配对差**：`d_i = cand_i - ref_i`（maximize）或 `d_i = ref_i - cand_i`（minimize），统一「正值 = candidate 更好」。
- **重采样**：`n_bootstrap` 次有放回按行重采样（固定 `seed` 可复现），每次求 `mean(d_resampled)`，得 mean-diff 的 bootstrap 分布。
- **CI**：percentile 区间（`ci_level` 缺省 0.95 → 2.5%/97.5% 分位）。
- **p-value**：单侧 `p_value = P(mean_diff ≤ 0)`（「candidate 不优于 reference」的概率）。
- **winner**：CI 下界 > 0 → `"candidate"`；CI 上界 < 0 → `"baseline"`；否则 `"tie"`。
- **Bayesian 增强（可选）**：`p_improvement = P(mean_diff > 0)`（bootstrap 分布中 > 0 的比例），作为连续风险信号随 `ComparisonVerdict` 一并产出（扩展字段或独立记录，见 §8 决策）。
- **信任边界**：`Comparator` 只消费 trusted evaluator 已产出的 per-sample 分数，**不读 labels、不重实现指标**；`aggregate` 语义由 `CandidateEvaluation` 契约保证（T1）。

### 4.2 接线

- **`supervisor.py`**：在 SOTA 比较处（现有 `_compare_metric` `:53-64` 与 `complete_experiment` `:744-754`）接入 `Comparator`：
  - 有 `candidate.per_sample_ref` 与 `reference.per_sample_ref` → `Comparator.compare(...)` 产出 `ComparisonVerdict(winner, p_value)`；`complete_experiment(..., verdict=verdict)` 不再恒 `None`。
  - 任一方缺 per-sample → 确定性回退到 `_compare_metric` 的方向比较，`verdict=None`（或仅由方向构造 `winner`，见 §8）。
- **`EvalResult`/`Experiment`**（`research_models.py`）：`ComparisonVerdict` 已有 `winner/p_value`；如引入 `p_improvement`，在 `ComparisonVerdict` 增可选字段（契约测试同步）。
- **喂 BT strength（交叉引用 search-selection 任务 D）**：`ComparisonVerdict.p_value` 作为 `Selector.strength` 的连续输入（如 `strength ∝ 1 - p_value`），或 `winner` 作为 `Outcome` 的统计版；在 search-selection 任务 D2 的 family 聚合中用 `p_value` 取代纯 `WIN/DRAW/LOSS` 计数。**本计划只产出数据与接口，不重写 `Selector`**（Selector 消费逻辑归 search-selection 任务 D）。

### 4.3 测试

- `test/unit/research/supervisor/test_comparator.py`（新）：固定 seed 可复现；方向对称（maximize/minimize 互换后 winner/p_value 对称）；CI 与 p-value 数值范围；「明显更优 / 明显更差 / 持平」三档的 winner 正确；空/不对齐输入报错。
- `test/unit/research/supervisor/test_supervisor.py`：`complete_experiment` 在有 per-sample 时写真实 `verdict`、缺 per-sample 时回退 `verdict=None`。
- 集成（`test/integration/research/test_rolling_search.py` 或新）：一次完整 SEARCH 结算产生非空 `verdict.p_value`（slow 或 mock evaluator）。

### 4.4 验收

```bash
uv run pytest test/unit/research/supervisor/test_comparator.py \
  test/unit/research/supervisor/test_supervisor.py -q
```

## 5. 依赖与顺序

```
T1（per-sample 契约） ──→ T2（Comparator + 接线）
                              │
                              └─→ search-selection 任务 D2（BT strength 消费 p_value）
```

- **T1 先于 T2**：Comparator 依赖 per-sample 分数；T1 的 `aggregate == primary` 契约是 bootstrap 可比性的前提。
- **依赖 search-selection 任务 B**：`math.isclose` tie 语义（`contracts.TIE_REL_TOL/TIE_ABS_TOL`）先稳定，Comparator 的 `aggregate ≈ primary` 校验与「tie 判定」才有一致数值基准。
- **T2 产出供 search-selection 任务 D2 消费**：本计划不反向改 `Selector`；两计划合并顺序建议 `search-selection(A→B) → statistical-rigor(T1→T2) → search-selection(D1→D2)`。
- 每任务独立提交，避免跨任务混改。

## 6. 验证命令（全量）

```bash
# 阶段一（per-sample 契约）
uv run pytest test/unit/research/test_evaluation.py \
  test/unit/research/supervisor/test_supervisor.py \
  tests/test_core_model_contracts.py -q

# 阶段二（Comparator + 接线）
uv run pytest test/unit/research/supervisor/test_comparator.py \
  test/unit/research/supervisor/test_supervisor.py -q

# 全量（默认排除 slow）
uv run pytest -q tests test/unit

# 涉及真实 LLM eval 脚本的 per-sample 用例（如新增 slow）
uv run pytest -q -m slow test/unit/research/ test/integration/research/
```

## 7. 验收标准（逐任务）

- [ ] T1：`CandidateEvaluation.per_sample_ref` 存在；`TrustedEvaluator.score` 有 per-sample 时回填 ref、缺失/不符时回退 `None` 且不伪造；`EvalResult.per_sample` 填 `per_sample_ref` 而非 `evidence_ref`。
- [ ] T2：`Comparator` 固定 seed 可复现、方向对称；`complete_experiment` 在有 per-sample 时写真实 `ComparisonVerdict(winner, p_value)`，缺时回退；平台 Comparator 全程不读 labels、不重实现指标。
- [ ] BT 输入就绪：`ComparisonVerdict.p_value` 可被 search-selection 任务 D2 消费（接口与数据存在，Selector 消费逻辑属该计划）。
- [ ] （可选）Bayesian：`p_improvement` 随 verdict 产出、数值 ∈ [0,1]、可复现。

## 8. 决策点与风险

- **bootstrap 合同写入 EvalSpec**（`supervisor_design.md:290` 的要求）：`seed`（缺省固定常量，如 0）、`n_bootstrap`（缺省 1000，受成本预检上限约束）、`ci_level`（缺省 0.95）三个字段冻结进 EvalSpec（或 PlanInput/state），execution 启动后不可改；本次只定义合同与缺省值，不改 EvalSpec 既有字段（避免与 search-selection 任务 B 的 tie 常量混改）。
- **paired 重采样在平台侧 vs eval 脚本侧**：**取平台侧**。平台侧是纯统计、不碰 labels、可审计；eval 脚本侧会把统计塞进不可信 LLM 脚本，破坏「trusted evaluator 唯一读 labels、平台不复刻指标」边界。
- **`min_effect` 是否引入**：设计 §2.4（`supervisor_design.md:271`）明确「不设置业务 `min_effect`」。**本次不引入**；bootstrap 的 CI/p-value 只作证据，不作为额外接受门槛。
- **显著性阈值与 tie tolerance 的关系**：两者分层——`math.isclose` tie tolerance（数值噪声，search-selection 任务 B）**先于**统计显著性判定「平局」；p-value/CI 用于「非平局但差异统计上不显著」时给 BT strength 一个**连续**信号，而**不改变 SOTA 接受规则**（SOTA 仍按方向 + tie tolerance 确定性比较，设计 §2.4「SOTA 只按 test primary score 比较」）。避免用 p 阈值硬卡 SOTA。
- **信任边界（红线）**：Comparator / Supervisor 不得读 test/final labels、不得接触指标公式；per-sample 分数只能来自 trusted evaluator。任何需要 labels 的新统计都必须下沉到 eval bundle 产出（如分类的逐行 0/1、回归的逐行误差），平台只做重采样。
- **`per_sample` 语义历史包袱**：`EvalResult.per_sample` 当前被误填 `evidence_ref`（`supervisor.py:749`）。T1 纠正语义后，旧 `.athena/research_tree.json`（v3）中已存字段仍是旧证据 ref；`ResearchTree.from_dict` 不做回填迁移（保持 append-only，不重写历史），新实验按新语义写。
- **回退纪律**：per-sample 缺失、`aggregate ≠ primary`、bootstrap 样本过小（如行数 < 2）等一律确定性回退到单点 `test_score` 比较，不伪造 CI/p-value。
- **文档维护**：实现并验证后，按 `docs/README.md` 维护规则更新 `docs/architecture/`（如涉及）与本文档 Status 为 completed，并在 search-selection 任务 D 落地时回填交叉引用状态。

## 9. 规范引用

- 设计基线：`docs/supervisor_design.md` §2.4（SOTA 比较、tie tolerance `math.isclose`、单一 SOTA、K-fold 证据策略）、§2.5（VALIDATE 泛化差距、`generalization_warning`）；`TODO(search-bootstrap)` 见 `:287-291`。
- 关联计划：`docs/dsh_docs/search-selection-upgrade-plan.md`（任务 B tie tolerance；任务 D BT strength，本计划产出其统计输入）。
- 代码规范：`docs/代码规范.md`（导入 / 注释 / 异常 / 格式 / 测试约定；black 88 列）。
- 规范 owner 声明：`docs/README.md`（`athena.core.research_models` 拥有 `ComparisonVerdict`/`EvalResult`；evaluator 契约属 `research/contracts.py`；SOTA 比较属 `research/supervisor`）。

# 搜索选择与比较子系统升级 — 实施计划

Status: 待实施（active）
Owner: Athena maintainers
Last verified: 2026-08-11
Scope: SEARCH 阶段的假设选择（Selector）与实验比较（SOTA / verdict / patience）
Source of truth: `src/athena/research/supervisor/{ranker,policy,scheduler,experiment,supervisor}.py`、
`src/athena/research/validation.py`、`src/athena/core/research_models.py`、`src/athena/gui/graph.py`

## 1. 目标与成功标准

把 SEARCH 的「选哪个假设做实验」与「新实验是否算改进」从当前多处不一致 / 退化的实现，升级为：

1. **正确性**：GUI 快览排序与 Scheduler 真实选序一致；比较按设计基线的 `math.isclose` 平局语义判定，数值噪声不再被误判为 WIN/LOSS。
2. **有效性**：冷启动 prior 从三行启发式升级为 ReflectionAgent 多准则打分；strength 信号从「对所有候选恒定的退化 Elo」升级为 lineage/family 的历史胜负强度。

**成功标准**：`uv run pytest -q tests test/unit src/athena/gui/test_graph.py` 全绿（排除 slow）；Selector 排序在新旧测试下方向一致且可复现；无 `model` 时仍按确定性回退路径工作，绝不静默伪造 rubric / strength。

## 2. 背景：当前实现的精确状态（含证据）

| # | 现状 | 证据 | 问题 |
|---|---|---|---|
| A | `gui/graph.py:rank_pending` 按 `(priority, order)` **升序** | `gui/graph.py:210-221`；`gui/test_graph.py:161-165` 断言 `["hyp4","hyp3"]`（priority 10 < 1000） | 与 Elo 语义（WIN +16，越高越强）及 Scheduler 的 `-priority` 降序**相反** |
| B | `supervisor.py:_compare_metric` 用单绝对 `delta > tolerance`（默认 0.0）判 WIN/LOSS | `supervisor.py:53-64`；`plans.py:123` `tolerance=0.0`；`experiment.py:203-233` `_better/apply_trusted_score` 无 tolerance | 默认 0.0 下 `1e-13` 差异被算作 WIN，违反设计 §2.4 的 `math.isclose(rel=1e-9, abs=1e-12)` |
| C | `ranker.py:rubric_prior` 仅 `0.4 + 0.3·(有 sources) + 0.3·(intervention ≥3 词)` | `ranker.py:40-50` | 无信息量，设计要 ReflectionAgent 多准则 |
| D | `policy.py:EloPolicy.settle` 固定期望分 0.5，`candidate = parent ± 16`；`Selector._normalized_strengths` 对所有 pending 兄弟恒定（seed=parent.priority） | `policy.py:32-57`、`ranker.py:141-155`、`supervisor.py:759` | strength 对排序**零贡献**（详见 §5） |
| — | `verdict` 恒为 `None`，无 Comparator / `p_value` | `supervisor.py:751`、`research_models.py:77-81`、全局 grep 无 p_value 生产者 | BT 的"真实两两比较"数据缺失 |

## 3. 阶段一：正确性修复（先做，独立、低风险）

### 任务 A — 修正 `rank_pending` 排序方向

- **改动** `src/athena/gui/graph.py:210-221`：排序键改为 `key=lambda h: (-h.priority, h.order if h.order is not None else 0)`；docstring 改为「priority 降序、order 升序（对齐 Scheduler 的 `queue_order`）」。
- **语义对齐**：`rank_pending` 是「待选假设按优先级」的**显示快览**，须与 `policy.queue_order`（`-priority, order`）一致；权威加权选序仍是 `Selector.rank`（`-score, order`），本次不改。
- **测试**：更新 `src/athena/gui/test_graph.py:161-165` 的 `test_rank_pending_by_priority`：期望改为 `["hyp3", "hyp4"]`（priority 1000 先于 10），并修正注释「priority 越大越强」。
- **验收**：`uv run pytest src/athena/gui/test_graph.py::test_rank_pending_by_priority -q`。

### 任务 B — 平局判定改用 `math.isclose`

- **抽公共常量**：在 `src/athena/research/contracts.py` 新增 `TIE_REL_TOL = 1e-9`、`TIE_ABS_TOL = 1e-12`；`validation.py:13-15` 的 `_TIE_REL_TOL/_TIE_ABS_TOL` 改为从 contracts 导入（单一 owner）。
- **改动** `supervisor.py:_compare_metric`：先判 `math.isclose(candidate, reference, rel_tol=TIE_REL_TOL, abs_tol=TIE_ABS_TOL)` → `Outcome.DRAW`；否则按 direction 判 WIN/LOSS。`tolerance` 保留为**可选 min_effect 叠加带**（`delta > tolerance` 才 WIN，默认 0.0 = 无 min_effect，符合设计「不设业务 min_effect」）。
- **改动** `experiment.py:_better/apply_trusted_score`：`apply_trusted_score` 增加 `tolerance` 形参，`run_turn` 传入 `plan_input.tolerance`；用同一 `isclose` 平局规则，使「plan 内 best 追踪」不再把数值噪声计为改进（不重置 `stale_rounds`）。
- **测试**：`test/unit/research/supervisor/test_policy.py` 或 `test_supervisor.py` 增用例：`0.84 vs 0.84+1e-13 → DRAW`、`0.84 vs 0.85 (tolerance=0) → WIN`、`minimize` 方向对称、`tolerance=0.05` 下 `0.84 vs 0.88 → DRAW`；`test_experiment.py` 增「噪声不重置 stale_rounds」用例。
- **验收**：`uv run pytest test/unit/research/supervisor/test_supervisor.py test/unit/research/supervisor/test_experiment.py test/unit/research/test_validation.py -q`。

## 4. 阶段二：有效性升级（建立在阶段一之上）

### 任务 C — `rubric_prior` 升级为 ReflectionAgent 多准则打分

- **新增模型**（`src/athena/research/supervisor/rubric.py`，新文件）：
  ```python
  class HypothesisRubric(BaseModel):
      verifiability: float = Field(ge=0, le=1)
      historical_difference: float = Field(ge=0, le=1)
      eda_evidence: float = Field(ge=0, le=1)
      feasibility: float = Field(ge=0, le=1)
      cost_penalty: float = Field(ge=0, le=1)   # 越高越贵
      leakage_risk: float = Field(ge=0, le=1)   # 越高越危险
      confidence: float = Field(default=0.5, ge=0, le=1)

  def rubric_prior(r: HypothesisRubric) -> float:
      """确定性加权聚合到 [0,1]；权重可配置、缺省 0.25/0.20/0.20/0.15/-0.10/-0.10。"""
  ```
- **扩展 `Hypothesis`**（`research_models.py`）：新增可选 `rubric_score: float | None = None`（由 ReflectionAgent 打分后回填；无则保持 None 走回退）。
- **改动 `Selector.rubric_prior`**（`ranker.py`）：`hypothesis.rubric_score is not None` 时用 `rubric_score`，否则回退现有三行式（**确定性回退，不伪造**）。
- **接线 ReflectionAgent**：在 `reflection_agent.md` 增「假设 rubric 评审」小节（输入一批假设 + EDA/baseline 证据 refs，输出每假设的 `HypothesisRubric` JSON）；由确定性策略校验 schema 与证据引用后回填 `rubric_score`。**无 model 时**直接跳过 rubric、保持回退 prior（不静默伪造）。
- **测试**：`test/unit/research/supervisor/test_ranker.py` 增 `rubric_prior` 聚合、有/无 `rubric_score` 两分支；契约测试校验 `Hypothesis` 新字段。
- **验收**：`uv run pytest test/unit/research/supervisor/test_ranker.py tests/test_core_model_contracts.py -q`。

### 任务 D — `strength` 从退化 Elo 升级为 family/lineage BT 强度

**现状根因**（写进注释，避免后人误判）：每个 hypothesis 创建时 `seed(parent)=parent.priority`，settle 又只拿 `reference_priority` 做 `parent ± 16`，导致所有 pending 兄弟共享同一 priority → `_normalized_strengths` 归一化后恒为 0.5 → `strength_weight=0.3` 只贡献常数，对排序零作用。

- **子任务 D1 — 双向 Elo**（`policy.py`）：
  - `HypothesisPolicy.settle` 签名改为 `settle(candidate_priority, reference_priority, outcome) -> float`。
  - 新 `BradleyTerryPolicy.settle`：`E = 1/(1+10**((reference_priority - candidate_priority)/400))`；`R' = candidate_priority + k*(S - E)`，`S ∈ {1.0, 0.5, 0.0}`。**只更新 candidate**（reference 是已冻结祖先，rating 不变）。
  - 同步更新 `Scheduler.settle` 与 `supervisor.py:759` 调用点（传入 `hypothesis.priority` 作为 `candidate_priority`）。
- **子任务 D2 — family strength 信号**（`ranker.py` + `research_tree.py` 或 `policy.py`）：
  - 定义 **family = 直接父假设**（`Hypothesis.parent_id` 对应的父实验假设）；维护每个已 settle 假设的「子代胜负计数」（WIN/LOSS/DRAW 聚合）。
  - `Selector._score` 的 `strength` 改为：`family_rating = 1000 + 16·(wins - losses)`（或 BT mean）；pending 假设的 `strength = normalized(family_rating)`。
  - **冷启动平滑**：family 已 settle 子代数为 0 时 `strength` 权重退化为 `prior`（沿用设计 §2.4「prior → BT mean 随真实比较数量平滑」）；实现上 `strength = blend(prior, bt_mean, n_settled)`，`blend` 用 `n_settled/(n_settled + c)`（`c` 可配置，缺省 4）。
- **确定性 / 回退**：所有信号仍是 ResearchTree 的纯函数；无 BT 数据时退化为 prior；固定 seed 可复现。
- **测试**：`test/unit/research/supervisor/test_policy.py` 增双向 Elo 用例（强胜弱 +16 应**小于**弱胜强）；`test_ranker.py` 增 family strength 区分（同 prior 下，胜率高的 family 排前）、冷启动回退。
- **验收**：`uv run pytest test/unit/research/supervisor/test_policy.py test/unit/research/supervisor/test_ranker.py test/unit/research/supervisor/test_scheduler.py -q`。

## 5. 依赖与顺序

```
任务 A ─┐
任务 B ─┼─→（阶段一，可并行，先合并）
任务 C ─┼─→（阶段二，依赖 B 的 tie 语义稳定，但可独立开发）
任务 D ─┘  （依赖 B；D1 先于 D2）
```

- A/B 不互相依赖，可同时开工；C 依赖 B（统一 tie 语义后 rubric 才可靠）；D 依赖 B + D1→D2 顺序。
- 合并顺序建议：A → B →（C、D1）→ D2。每任务独立提交，避免跨任务混改。

## 6. 验证命令（全量）

```bash
# 阶段一 + 图预览
uv run pytest src/athena/gui/test_graph.py test/unit/research/supervisor/test_supervisor.py \
  test/unit/research/supervisor/test_experiment.py test/unit/research/test_validation.py -q

# 阶段二（selector/policy/scheduler + 契约）
uv run pytest test/unit/research/supervisor/test_ranker.py test/unit/research/supervisor/test_policy.py \
  test/unit/research/supervisor/test_scheduler.py tests/test_core_model_contracts.py -q

# 全量（默认排除 slow）
uv run pytest -q tests test/unit

# 涉及真实 LLM 的 rubric 用例（如新增 slow）
uv run pytest -q -m slow test/unit/research/supervisor/
```

## 7. 验收标准（逐任务）

- [ ] A：`rank_pending` 输出与 `queue_order` 同向；`test_rank_pending_by_priority` 更新后通过。
- [ ] B：`_compare_metric` 对数值噪声判 DRAW；`apply_trusted_score` 不因噪声重置 `stale_rounds`；`tolerance` 作为可选 min_effect 保留。
- [ ] C：`rubric_prior` 在有 `rubric_score` 时用多准则、无时回退三行式；无 model 不伪造 rubric。
- [ ] D：`strength` 对 pending 兄弟产生可复现的区分（family 胜率差异 → 排序差异）；双向 Elo 强弱对称正确。

## 8. 决策点与风险

- **`tolerance` 语义**（任务 B）：保留为「可选 min_effect 叠加带」而非删除，兼容现有 GUI「容忍度」设置与已冻结 `PlanInput`；数值平局由 `math.isclose` 独立保证。
- **family 定义**（任务 D2）：取「直接父假设」而非「lineage 根」，避免整棵树同源导致 family 退化为单桶；如后续需要更粗粒度，可在 `Hypothesis` 加显式 `family` 字段（本次不加，控制范围）。
- **真实 BT 依赖 Comparator**：当前 `verdict` 恒 None、无 `p_value`。本次用 `_compare_metric` 的 WIN/DRAW/LOSS 作为 BT 输入；「paired bootstrap / 显著性 p_value」留待独立批次（对应 brainstorm §4.1 `TODO(search-bootstrap)`），不在本计划实现。
- **回退纪律**：任何升级不得破坏「无 model / 无 rubric / 无历史比较」时的确定性回退；测试须覆盖三条回退路径。
- **文档维护**：实现并验证后，按 `docs/README.md` 维护规则更新 `docs/architecture/`（如涉及）与本文档 Status 为 completed。

## 9. 规范引用

- 设计基线：`docs/supervisor_design.md` §2.4（Selector 两路信号、BT/UCB、math.isclose tie、单一 SOTA）。
- 代码规范：`docs/代码规范.md`（导入 / 注释 / 异常 / 格式 / 测试约定；black 88 列）。
- 规范 owner 声明：`docs/README.md`（`athena.core.research_models` 拥有 `Hypothesis` 等模型；rank/比较逻辑属于 `research.supervisor`，GUI 图算法属 `gui/graph.py`）。

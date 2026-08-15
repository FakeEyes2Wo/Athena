# 图补全与消融归因 — 实施计划

Status: 待实施（active）
Owner: Athena maintainers
Last verified: 2026-08-11
Scope: ResearchGraph 的 `depends_on` 边补全 + VALIDATE 阶段的消融（依赖闭包 leave-one-out）+ Shapley 近似归因
Source of truth: `src/athena/core/research_models.py`、`src/athena/core/research_tree.py`、
`src/athena/research/contracts.py`、`src/athena/research/validation.py`、
`src/athena/research/supervisor/validation.py`、`src/athena/gui/graph.py`

## 1. 目标与成功标准

把「图补全」批次落地为：给 Hypothesis 增加显式 `depends_on` 依赖边，在 VALIDATE 实现
FULL_LINEAGE 的依赖闭包 leave-one-out 消融，并在此之上加入 Shapley 近似归因作为增强。

1. **图补全**：`Hypothesis` 拥有 `depends_on` 边，与 `supersedes` 语义严格区分；`ResearchTree`
   校验 `depends_on` 只指向祖先、无环、无重复、无自指；`gui/graph.py` 输出该边与传递闭包。
2. **消融**：进入 VALIDATE 后、final_test 之前，对 SOTA lineage 上每个 accepted intervention
   执行一次依赖闭包 leave-one-out，产出结构化 `AblationRecord`（含 `ablation_kind`），聚合为
   `AblationSummary`；失败可降级为 `INCONCLUSIVE`。
3. **Shapley 归因**：在 FULL_LINEAGE 之上给出干预贡献排序，与 leave-one-out `score_delta` 方向一致。

**成功标准**：`uv run pytest -q tests test/unit src/athena/gui/test_graph.py` 全绿（排除 slow）；
消融在 final_test 之前完成、只在 test 上评分、**不读 final_test、不改冻结 SOTA / 排行榜**；`depends_on`
图校验被测试覆盖；Shapley 归因可复现（固定 seed）且在成本上限内不额外重训。

## 2. 背景：当前实现的精确状态（含证据）

| # | 现状 | 证据 | 问题 |
|---|---|---|---|
| A | VALIDATE 编排只有 reproduce SOTA → final_test score → gap，**无任何 ablation** | `supervisor/validation.py:1-528`（`run_validation_plan` 378-528、`_score_result` 325-375、`recovery_action` 80-116） | 缺设计 §2.5 的消融 |
| B | `ValidationService.build_result` 只算 gap/warning | `research/validation.py:46-70`；`research/contracts.py:46-59`（`ValidationResult`） | 无 `AblationRecord`/`AblationSummary` 合同 |
| C | `Hypothesis` 有 `supersedes`，**无 `depends_on`** | `research_models.py:18-40`（`supersedes` 在 30） | 缺依赖边，无法表达"依赖祖先" |
| D | `ComparisonVerdict(winner,p_value)` 已定义但无人生产 | `research_models.py:77-81`；`supervisor.py:751` `verdict=None` | 无两两比较数据（不阻塞本计划） |
| E | `contracts.py` 无消融合同 | `research/contracts.py:46-59` | 缺 `AblationRecord`/`AblationSummary` |
| F | 现成 lineage/图遍历可复用 | `research_tree.py:274` `active_hypotheses`、`294` `_validate_supersedes`；`gui/graph.py:224` `supersedes_closure`、`243` `refutation_reachability`、`138` `cycle_detect` | 依赖闭包/环检测可仿照 |

## 3. 阶段一：`depends_on` 边 + `ResearchTree` 校验（T1）

### 任务 T1 — 给 `Hypothesis` 加 `depends_on` 并校验

- **改动** `src/athena/core/research_models.py`：`Hypothesis` 新增
  `depends_on: list[str] = Field(default_factory=list)`（放在 `supersedes`（30 行）之后）。
- **改动** `src/athena/core/research_tree.py`：
  - 新增 `_validate_depends_on(child, experiment_id, *, ancestors=None)`，仿 `_validate_supersedes`（294 行）：
    - `depends_on` 无重复、不含自身 id；
    - 每个 id 必须落在选中父实验谱系（`hypotheses_path`）上；
    - **语义区分**（写进 docstring）：`supersedes` = 子假设**取代/替换**祖先干预（后代不再需要它）；
      `depends_on` = 子假设**依赖**祖先干预才能成立（消融时须一并移除）。二者可并存、集合不必相交。
  - `add_hypothesis`（119 行）在 `_validate_supersedes` 之后调用 `_validate_depends_on`；`from_dict`（411 行）同样校验。
  - 环安全：`depends_on` 只指向祖先天然无环，校验只防自指/重复/非祖先。
- **改动** `src/athena/gui/graph.py`：
  - `build_hypothesis_graph`（18 行）新增第三种边 `depends_on`（`子 → 被依赖祖先`，与 `supersedes` 同向）；
  - 新增 `depends_on_closure(tree, {hypothesis_id})`（仿 `supersedes_closure` 224 行）返回传递闭包；
  - `cycle_detect`（138 行）把 `depends_on` 作为独立边种类检测无环。
- **测试**：`tests/test_research_tree_v2.py`、`test/unit/research/test_research_tree_scheduling.py` 增 depends_on
  祖先约束/无环/去重/自指拒绝/与 supersedes 并存；`src/athena/gui/test_graph.py` 增 depends_on 边 + 闭包 + 环检测。
- **验收**：`uv run pytest tests/test_research_tree_v2.py test/unit/research/test_research_tree_scheduling.py src/athena/gui/test_graph.py -q`。

## 4. 阶段二：VALIDATE FULL_LINEAGE leave-one-out 消融（T2）

### 任务 T2 — 依赖闭包 leave-one-out 消融

- **改动** `src/athena/research/contracts.py`：新增（对齐设计 §2.5 418-428）：
  ```python
  class AblationRecord(BaseModel):
      intervention_id: NonBlankText
      requested_intervention_id: NonBlankText
      removed_dependency_closure: list[str] = Field(default_factory=list)
      ablation_kind: Literal["single", "dependency_group"]
      full_sota_test_score: float
      ablated_test_score: float | None = None   # INCONCLUSIVE 时为空
      score_delta: float | None = None
      train_duration: float = Field(default=0.0, ge=0.0)
      status: Literal["SUCCEEDED", "INCONCLUSIVE"]
      artifact_refs: dict[str, ArtifactRef] = Field(default_factory=dict)

  class AblationSummary(BaseModel):
      status: Literal["COMPLETE", "PARTIAL"]
      planned: int
      succeeded: int
      inconclusive: int
      coverage_ratio: float
      failures: list[str] = Field(default_factory=list)
      risk_note: str = ""
      shapley_ranking: dict[str, float] | None = None   # T3 回填
  ```
- **新增** `src/athena/research/supervisor/ablation.py`：
  - `dependency_closure(tree, intervention_id) -> list[str]`：requested + 全部 `depends_on` 传递闭包（复用 T1 的 `depends_on_closure`）；
  - `leave_one_out_variant(sota, removed)`：构造"移除该干预 + 闭包"的解释性变体，复用 VALIDATE 的
    reproducibility preflight 思路 + `DataScriptBundle` + `TrustedEvaluator`，**在 test 上评分、不读 final_test**；
  - 编排：对 SOTA lineage 每个 accepted intervention 生成一个 `AblationRecord`（闭包长度 =1 → `single`，>1 → `dependency_group`）；
  - 失败分层（对齐 §2.5 443-453）：reproducibility preflight 失败 → SOTA validation failure（execution FAILED）；
    "移除后的变体"失败 → `status=INCONCLUSIVE`（`ablated_test_score` 空），batch 继续；
  - `build_summary(records)`：聚合为 `AblationSummary`；有 INCONCLUSIVE → `PARTIAL`。
- **接线** `src/athena/research/supervisor/validation.py`：在 `run_validation_plan` 的 final-test 之前插入
  ablation gate（或作为独立 `run_ablation_phase`，由 `ResearchRuntime`/`Supervisor` 在 VALIDATE 进入时调用）。
  **约束**：ablation 完成并提交 `AblationSummary` 后才允许 final-test Plan；ablation 不调用 `set_sota`、
  不写 `Experiment.eval`、不改 `best_experiment_id`。
- **范围外（本次不实现）**：`BASELINE_ONLY` 的 Human gate（`ablation_scope` HumanRequest）、final-test
  唯一 attempt 状态机（`RESERVED→…→COMMITTED`）——本计划只保证「ablation 在 final-test 前、不改 SOTA」。
- **测试**：`test/unit/research/supervisor/test_validation_plan.py` 或新 `test/unit/research/supervisor/test_ablation.py`
  增：2-3 节点 lineage 的依赖闭包、`single`/`dependency_group` 区分、INCONCLUSIVE 降级、ablation 不改 SOTA；
  契约测试 `test/unit/research/test_validation.py`、`tests/test_core_model_contracts.py` 校验新 schema。
- **验收**：`uv run pytest test/unit/research/supervisor/test_validation_plan.py test/unit/research/test_validation.py tests/test_core_model_contracts.py -q`。

## 5. 阶段三：Shapley 近似归因（T3，增强）

### 任务 T3 — Shapley 归因贡献排序

- **新增** `src/athena/research/supervisor/shapley.py`：
  - 输入：SOTA lineage 上 N 个 accepted intervention + T2 已产生的 leave-one-out 边际分数；
  - **算法选择：Monte Carlo Shapley（permutation sampling）**，用 `v({i})`（leave-one-out 单点边际）、
    `v(∅)`（baseline）、`v(N)`（全量 SOTA）作锚点，按随机排列采样边际贡献；
  - **依赖闭包分组**：把每个 intervention 与其 `depends_on` 闭包打包成"原子"，避免"移除 A 却保留依赖 A 的 B"
    的非法变体（对应 `ablation_kind=dependency_group`）；
  - **成本上限**：`max_shapley_permutations`（缺省 `2*N`，可配置）与 `max_shapley_train_duration`；默认**复用
    leave-one-out 分数只做重加权聚合、不额外重训**；超限退化为纯 leave-one-out 边际（不产 Shapley）。
  - **确定性**：固定 seed 的排列采样，可复现。
  - 输出：`{intervention_id: shapley_value}` 贡献排序 + 与 `score_delta` 的对照表；结果回填
    `AblationSummary.shapley_ranking`。
- **约束**：Shapley 是 FULL_LINEAGE 之上的增强，只读 test、不读 final_test、不改 SOTA；不新增训练
  （除非在 `max_shapley_permutations` 预算内显式开启 subset 重训，缺省关闭）。
- **测试**：新 `test/unit/research/supervisor/test_shapley.py`：小图（N≤3）与穷举 Shapley 精确值对照；
  依赖闭包分组的原子性；成本上限触发退化为 leave-one-out；固定 seed 可复现。
- **验收**：`uv run pytest test/unit/research/supervisor/test_shapley.py -q`。

## 6. 依赖与顺序

```
T1（depends_on 边 + 校验）──► T2（leave-one-out 消融，依赖闭包读 depends_on）
                                  │
                                  └──► T3（Shapley 归因，复用 T2 变体分数）
```

- T1 是 T2/T3 的图基础，必须先做。
- T2 依赖 T1（依赖闭包读取 `depends_on`）；T3 依赖 T2（复用 leave-one-out 分数）。
- 每任务独立提交，避免跨任务混改。

## 7. 验证命令（全量）

```bash
# T1（模型 + 树校验 + 图遍历）
uv run pytest tests/test_research_tree_v2.py test/unit/research/test_research_tree_scheduling.py \
  src/athena/gui/test_graph.py -q

# T2（消融 + 契约）
uv run pytest test/unit/research/supervisor/test_validation_plan.py \
  test/unit/research/test_validation.py tests/test_core_model_contracts.py -q

# T3（Shapley）
uv run pytest test/unit/research/supervisor/test_shapley.py -q

# 全量（默认排除 slow）
uv run pytest -q tests test/unit
```

## 8. 验收标准（逐任务）

- [ ] T1：`Hypothesis.depends_on` 落盘/加载；`depends_on` 只指向祖先、无环、无重复、无自指；与 `supersedes`
  语义区分并有并存用例；`gui/graph.py` 输出 depends_on 边 + 闭包 + 环检测。
- [ ] T2：FULL_LINEAGE 对 lineage 每个 accepted intervention 生成 `AblationRecord`；闭包正确
  （`dependency_group` 时移除完整闭包）；失败降级 `INCONCLUSIVE` 且 `ablated_test_score` 空；ablation
  完成后才 final-test，且不改 SOTA/排行榜。
- [ ] T3：Shapley 输出贡献排序与 leave-one-out `score_delta` 方向一致；依赖闭包按原子打包；成本上限触发
  退化为 leave-one-out；固定 seed 可复现。

## 9. 决策点与风险

- **`depends_on` vs `supersedes` 语义区分**（T1，显式）：
  - `supersedes` = 子假设**取代/替换**祖先干预（`active_hypotheses` 据此减去 superseded 祖先）。
  - `depends_on` = 子假设**依赖**祖先干预（消融时须**一并移除**依赖闭包，否则变体非法）。
  - 两者边方向相同（子→祖先）但语义正交：supersedes 影响"活跃假设集"，depends_on 影响"消融移除闭包"。
- **Shapley 算法与成本上限**（T3，显式）：Monte Carlo Shapley + 依赖闭包分组；默认复用 leave-one-out
  分数、不额外重训；`max_shapley_permutations`/`max_shapley_train_duration` 硬上限；超限退化为 leave-one-out。
- **ablation 不读 final_test / 不改 SOTA**（T2/T3，显式）：ablation 只在 test 上评分；不调用 `set_sota`、
  不改 `Experiment.eval`、不改 `best_experiment_id`；`AblationRecord` 写独立 fact。
- **ablation 失败如何降级**（T2，显式）：reproducibility preflight 失败 = execution `FAILED`（不得用
  partial-ablation gate 绕过）；"移除后变体"失败 = `INCONCLUSIVE`（`ablated_test_score` 空），batch 继续，
  summary=`PARTIAL`。
- **范围边界**（显式）：`BASELINE_ONLY` 的 Human gate、final-test 唯一 attempt 状态机、paired bootstrap
  `p_value` 均不在本计划；`ComparisonVerdict` 仍不生产（属后续 Comparator 批次）。

## 10. 规范引用

- 设计基线：`docs/supervisor_design.md` §2.5（`ablation_mode` 405/430、`depends_on` 边 411-416、
  `AblationRecord` 字段 418-428、失败分层 443-453、`AblationSummary` PARTIAL 455-470、final_test 恰好一次 472-504）。
- 代码规范：`docs/代码规范.md`（导入 / 注释 / 异常 / 格式 / 测试约定；black 88 列）。
- 规范 owner：`docs/README.md`（`athena.core.research_models` 拥有 `Hypothesis`/`ComparisonVerdict`；
  `athena.research.contracts` 拥有 VALIDATE 合同；消融编排属 `research.supervisor`；图遍历属 `gui/graph.py`）。

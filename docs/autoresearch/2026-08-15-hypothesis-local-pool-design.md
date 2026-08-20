# Hypothesis 本地池设计（HypothesisPool）

日期：2026-08-15
状态：设计文档（待实现）
包归属：`@athena/autoresearch`（`hypothesisPool` 服务）
范围：AutoResearch / Athena 的假设生命周期管理，与 `ResearchTree` 协同
Source of truth 参考：`src/athena/core/research_models.py`、`src/athena/core/research_tree.py`、
`src/athena/research/supervisor/{supervisor,scheduler,ranker,policy}.py`、`src/athena/research/idea_generation/`

---

## 0. 2026-08-20 ML v1 实现范围

本文的生命周期语义、四段 ID 追溯、阴性结果保留以及 ResearchTree/Pool 事实边界
继续有效。为控制首版代码量，ML v1 只实现 candidate-to-paper 和自主循环实际读写
的字段、状态和查询：

- 不预先实现全部概念查询 API、归档拆分和跨领域扩展字段；
- 不为每个状态建立单独类或复杂状态机框架，使用校验后的 JSON 记录即可；
- Athena `ResearchTree` 仍是实验事实源，Pool 只保存跨阶段生命周期和论文元数据；
- 新计划和新假设追加记录，不回改已完成实验及其 evidence；
- 第二领域接入时再根据真实差异扩展模型。

控制平面只通过少量 Pool 操作支持 ideation、experiment、evidence 和 publication，
不把 HypothesisPool 扩张为新的调度器。

## 1. 背景与问题

当前 Athena 的假设只存在于 `ResearchTree`：

- `Hypothesis` 记录 `PROPOSED / SUPPORTED / REFUTED / INCONCLUSIVE / REJECTED` 状态。
- `pending_hypotheses()` 只取 `PROPOSED`，是 SEARCH 调度器的唯一候选来源。
- 假设结算后，`SUPPORTED / REFUTED / INCONCLUSIVE` 都留在树里，但没有“哪些进入论文”“哪些是阴性结果但值得写”“哪些被后续假设取代”这些 AutoResearch 需要的信息。
- `Hypothesis` 本身不保存生成来源、门禁报告、论文归属、实验指标对比等横向生命周期字段。

AutoResearch 从 idea 生成走到 paper generation，需要跨阶段追踪同一个假设的完整生命周期：

```
生成(idea) → 入池(queued) → 被实验(selected/running) → 出结果(supported/refuted/inconclusive)
    → 论文取舍(promoted_to_paper / negative_results) → 归档(archived/superseded)
```

## 2. 目标 / 非目标

### 目标

1. 在 `ResearchTree` 之外增加一个**独立的 Hypothesis 本地池**，持久化为 `hypothesis_pool.json`。
2. 池保存候选假设的**生成来源、门禁审计、实验结论、论文归属**，且不破坏现有 `ResearchTree` 契约。
3. 池与 `ResearchTree` 可双向对账：树是实验图的事实源，池是生命周期/论文元数据的事实源。
4. 为 AutoResearch 的 Idea Generation、Experiment、Paper Writing、Paper Refinement 提供统一查询入口。

### 非目标

- 不替换 `ResearchTree` 或 `Scheduler/Selector`；当前 Athena 无池时行为必须逐字节不变。
- 不引入 SQLite/数据库；先用 JSON 单文件 + 原子写，与 `state.json` / `research_tree.json` 一致。
- 不在本次实现任何代码（q9：只写设计文档）。

## 3. 核心模型

### 3.1 `PooledHypothesis`

池记录 = 核心 `Hypothesis` 快照 + 池生命周期字段。

```jsonc
{
  "pool_id": "hyp_1a2b3c4d5e6f",          // 复用 Hypothesis.id；树与池一一对应
  "hypothesis": {                          // core.Hypothesis 的 JSON 快照
    "statement": "...",
    "intervention": "...",
    "expected_effect": "...",
    "status": "SUPPORTED",
    "priority": 1016.0,
    "parent_id": "exp_baseline",
    "supersedes": [],
    "sources": ["paper://..."],
    "evidence_refs": ["artifact:..."],
    "order": 1
  },
  "pool_status": "PROMOTED_TO_PAPER",      // 见 3.2
  "origin": "live_eda_ideator",            // 见 3.3
  "origin_candidate_id": "cand-3f9a",      // candidate→paper 链路根 ID；非该链路为 null
  "generation_strategy": "eda_grounded",   // idea_generation 策略 id，可为 null
  "gate_summary": {                        // 门禁摘要（可空）
    "verdict": "PASS",
    "blocking_factor": null,
    "evidence_ref": "artifact:..."
  },
  "experiment_ids": ["exp_hyp_1a2b3c4d5e6f"],
  "best_metric": 0.86,
  "metric_direction": "maximize",
  "delta_vs_reference": 0.06,
  "comparison_outcome": "WIN",             // WIN/DRAW/LOSS/None
  "paper_refs": ["paper_draft.md", "sections/results.tex"],
  "paper_section_ids": ["results", "limitations"],
  "negative_result": false,
  "created_at": "2026-08-15T12:00:00Z",
  "updated_at": "2026-08-15T13:00:00Z"
}
```

### 3.2 `pool_status` 状态机

| 状态 | 含义 | 准入条件 |
|---|---|---|
| `QUEUED` | 已入池、等待被实验或排序 | 生成通过结构/门禁校验 |
| `SELECTED` | 已被 SEARCH 调度器选中，Plan 尚未终态 | `start_plan` 创建 Plan |
| `RUNNING` | 实验运行中 | Experiment 状态 `RUNNING` |
| `SUPPORTED` | 实验相对参考有 WIN | `_settle_plan` 判定 WIN |
| `REFUTED` | 实验为 DRAW/LOSS | `_settle_plan` 判定 DRAW/LOSS |
| `INCONCLUSIVE` | 无有效实验证据 | `_settle_plan` 无 best |
| `REJECTED` | 门禁拒绝且重试后仍拒绝 | 门禁 REJECT |
| `PROMOTED_TO_PAPER` | 已写入论文正文 | Paper Writing 引用 |
| `NEGATIVE_RESULT` | 实验为阴性但写入 limitations/negative results | Paper Writing 引用 |
| `SUPERSEDED` | 被后续假设取代 | 子假设 `supersedes` 命中 |
| `ARCHIVED` | 不进入论文且不再调度 | 人工/策略归档 |

状态迁移：

```
QUEUED → SELECTED → RUNNING → SUPPORTED/REFUTED/INCONCLUSIVE
                     RUNNING → INCONCLUSIVE（无 best）
QUEUED → REJECTED（门禁拒绝）
SUPPORTED/REFUTED/INCONCLUSIVE → PROMOTED_TO_PAPER / NEGATIVE_RESULT / ARCHIVED / SUPERSEDED
PROMOTED_TO_PAPER / NEGATIVE_RESULT → ARCHIVED
```

### 3.3 `origin` 来源枚举

| origin | 说明 |
|---|---|
| `live_eda_ideator` | SEARCH 内实时 EDA-grounded Ideator |
| `idea_generation_pipeline` | 离线全流程 `run_full_pipeline` |
| `manual` | 人工提出 |
| `paper_revision` | 论文修订阶段反向生成的补充假设 |
| `literature_gap_mining` | 空白挖掘直接产出 |

`origin_candidate_id`：candidate→paper 链路的根 ID（`cand-*`）。由 ideation 入池时写入；独立 Athena / records→paper 运行时为 `null`。evidence adapter 用它填充 `evidence_chain.experiments[].candidate_id`，实现四段 ID 追溯。

## 4. 存储与持久化

- 路径：`.athena/autoresearch/hypothesis_pool.json`（AutoResearch 启用时）。
- 当前 Athena 不启用 AutoResearch 时：**不创建池文件**，行为不变。
- 文件顶层结构：

```jsonc
{
  "version": 1,
  "entries": { "<pool_id>": { /* PooledHypothesis */ } }
}
```

- 写策略：单写者（AutoResearchSupervisor 或 HypothesisPool 服务）持锁；原子写复用 `atomic_write_json`。
- 读策略：启动时加载；运行中内存态 + 每次变更原子落盘。
- 对账（reconciliation）规则：
  - 池缺失但树有假设：从树导入，`pool_status` 由 `Hypothesis.status` 映射（`PROPOSED→QUEUED`、`SUPPORTED→SUPPORTED`、`REFUTED→REFUTED`、`INCONCLUSIVE→INCONCLUSIVE`、`REJECTED→REJECTED`）。
  - 池有但树没有：保留（尚未入树的候选）。
  - 树与池同一 `pool_id` 的 `status` 冲突：树为实验事实源，池更新树的结论；树的 `PROPOSED` 不覆盖池的 `REJECTED/ARCHIVED`。

## 5. 与 ResearchTree / Scheduler 的协作

### 5.1 入池时机

1. `register_hypotheses`（或 AutoResearch 的 Idea Generation 阶段）成功写入树后，逐个 upsert 池：`pool_status=QUEUED`。
2. 门禁拒绝的候选也入池：`pool_status=REJECTED`，保留拒绝理由，避免重复生成同样错误。
3. 去重：入池前用 `Selector.deduplicate` 的 Jaccard 阈值（0.8）与池中所有 `QUEUED/SELECTED/RUNNING` 条目比较；重复则只更新原条目 `updated_at` 并记录 `duplicate_of`。

### 5.2 调度选择

- 当前 `Scheduler._queued` 仍以树 `pending_hypotheses()` 为输入，**保持不变**。
- AutoResearch 可增加 `PoolAwareSelector`（概念）：先按池状态过滤（只 `QUEUED`），再用现有 `Selector` 排序；池只提供过滤和额外特征，不替代排名算法。
- 选中后：池 `QUEUED → SELECTED`；`start_plan` 后 `SELECTED → RUNNING`。

### 5.3 结算回写

`Supervisor._settle_plan` 后，AutoResearch 层监听同一结果并回写池：

- WIN → `SUPPORTED`；DRAW/LOSS → `REFUTED`；无 best → `INCONCLUSIVE`。
- 记录 `best_metric`、`delta_vs_reference`、`comparison_outcome`、`experiment_ids`。
- 若新 SOTA 胜出，将其树祖先链上的池条目标记 `SUPERSEDED`（仅池状态，不改树）。

### 5.4 论文回写

Paper Writing / Refinement 阶段：

- 引用某假设写正文 → `PROMOTED_TO_PAPER`。
- 作为阴性结果写入 limitations/negative results → `NEGATIVE_RESULT`。
- 明确不写 → `ARCHIVED`。

## 6. 查询 API（概念）

| 方法 | 返回 |
|---|---|
| `list(pool_status)` | 按状态过滤 |
| `queued()` | 所有 `QUEUED`，按 `hypothesis.priority` 降序 |
| `selected() / running()` | 运行中候选 |
| `supported() / refuted() / inconclusive()` | 已结算候选 |
| `promoted_to_paper()` | 已写入论文候选 |
| `negative_results()` | 阴性结果候选 |
| `get(pool_id)` | 单条记录 |
| `upsert(record)` | 插入/更新 |
| `reconcile(tree)` | 与 ResearchTree 对账 |
| `find_duplicates(text, threshold)` | Jaccard 查重 |
| `export_for_paper()` | 论文写作用的证据包 |

## 7. 不变量与校验

1. `pool_id` 必须等于 `Hypothesis.id`；池与树一一对应。
2. `pool_status` 只能按状态机迁移，非法迁移抛错。
3. `PROMOTED_TO_PAPER` 必须至少有一个 `paper_ref`。
4. `SUPPORTED/REFUTED/INCONCLUSIVE` 必须至少有一个 `experiment_id`。
5. 池文件顶层版本与字段同 `research_tree.json` 一样严格校验，未知字段拒绝。
6. 任何写池失败不得阻塞实验结算：池是增强索引，树是事实源；池写失败时记录告警，下次 `reconcile` 修复。

## 8. 后续实现计划（待批准后拆任务）

1. 在 `athena_ts/packages/athena-core` 或 AutoResearch 概念包中定义 `PooledHypothesis` zod schema 与 `pool_status` 状态机（纯模型层）。
2. 实现 `HypothesisPool` 类：load/save/upsert/reconcile/list 查询 + 原子写。
3. 在 AutoResearch 概念插件中注册 `hypothesisPool` 服务，并接入 Idea Generation 与 Experiment 结算两个写入点。
4. 在 Paper Writing 阶段消费 `export_for_paper()` 与 `negative_results()`。
5. 测试：状态机迁移、对账规则、与树冲突处理、查重、崩溃恢复。

## 9. 风险与开放项

- **双事实源漂移**：池与树状态可能不一致。缓解：树为实验事实源，池定期/启动 `reconcile`；池状态机不允许反向覆盖树的终态。
- **文件规模**：长周期运行后池可能膨胀。ML v1 不做拆分；只有真实文件规模影响加载
  或原子写时，才在 M4 根据测量结果把 `ARCHIVED` 条目拆到
  `hypothesis_pool.archive.json`。
- **与未来 MCTS/流式 Ideation 的关系**：池天然支持“任意父节点展开”，MCTS 设计中的候选管理可复用池；本设计不绑定 MCTS。
- 开放：是否允许同一假设在池中保留多个版本（修订版）？当前设计假设 `pool_id = Hypothesis.id` 一一对应，修订产生新 id。

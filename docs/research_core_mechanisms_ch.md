# Athena 研究核心机制整理：ResearchTree / SEARCH / IdeaGenerator / Hypothesis 排序 / 动态 EDA

> 状态：current
> 覆盖范围：`src/athena/core/research_tree.py`、`src/athena/core/research_models.py`、
> `src/athena/research/supervisor/`、`src/athena/research/idea_generation/`、
> `src/athena/research/agent_turn_runner.py`、`src/athena/research/phase_runner.py`、
> `src/athena/research/runtime.py`、`src/athena/agents/ideator_agent.py`、`src/athena/agents/data_agent.py`。
> 本文整理的是当前 `main` 分支的真实实现，不描述已删除或未接线的历史代码。

---

## 0. 全景：一次研究运行的生命周期

```
PREPARE ──> SEARCH ──> VALIDATE ──> COMPLETED
  │            │
  │            ├─ 动态 EDA：Ideator 提出 eda_request → Data Agent 补分析写回 EDA 目录
  │            └─ IdeaGenerator（Ideator Agent）产生 Hypothesis → 门禁过滤 → 入 ResearchTree → 排序调度
  │
  └─ 在固定名称 worktree（name="eda"）里做 EDA，冻结 evaluator，产出 baseline 并写入 ResearchTree 作为 SOTA
```

- 组合根：`src/athena/research/runtime.py` 的 `ResearchRuntime`。
- 唯一写者：`src/athena/research/supervisor/supervisor.py` 的 `Supervisor`，它独占 `ResearchState` 与 `ResearchTree` 的变更。
- 状态落盘：`.athena/state.json`（`ResearchState`）与 `.athena/research_tree.json`（`ResearchTree`）。

---

## 1. ResearchTree 机制

### 1.1 核心模型

文件：`src/athena/core/research_models.py`

| 模型 | 作用 |
|---|---|
| `Hypothesis` | 一个可证伪的 ML 假设。关键字段：`statement`、`intervention`、`expected_effect`、`status`、`parent_id`、`supersedes`、`priority`、`order`、`patience`、`turn_limit`、`cost`、`sources`、`evidence_refs` |
| `HypothesisStatus` | `PROPOSED`（待选）/ `SUPPORTED`（支持）/ `REFUTED`（证伪）/ `INCONCLUSIVE`（无有效证据）/ `REJECTED`（彻底拒绝） |
| `HypothesisBatch` | 普通 Ideator 输出：一组 `Hypothesis` + 可选 `eda_request`（触发动态 EDA） |
| `EdaResult` | Data Agent 动态 EDA 输出：`summary` |
| `ExperimentPlan` | 实验计划：`kind`、`change`、`run_config_ref`、`budget`、`acceptance_rule` |
| `EvalResult` | 评估结果：`experiment_id`、`primary`、`secondary`、`per_sample` |
| `ComparisonVerdict` | 两实验比较：`winner`（baseline/candidate/tie）+ `p_value` |

### 1.2 ResearchTree 本体

文件：`src/athena/core/research_tree.py`

`ResearchTree` 是实验图的内存对象，维护四类事实：

- `_hypotheses`：hypothesis_id → `Hypothesis`
- `_experiments`：experiment_id → `Experiment`
- `_children_index`：父实验 id → 子实验 id 列表
- `_sota_id`：当前 SOTA 实验 id

`Experiment`（同文件）是单次实验执行记录：

- 每个 `Experiment` 必须挂到一个 `Hypothesis`；一个 hypothesis 最多只能有一个 experiment（`experiment_for_hypothesis` 唯一）。
- `Experiment.parent_id` 必须与 `Hypothesis.parent_id` 一致，从而形成“父实验 → 子假设 → 子实验”的 lineage。
- `ExperimentStatus` 状态机：
  - `PENDING → {RUNNING, CANCELLED}`
  - `RUNNING → {SUCCEEDED, FAILED, CANCELLED}`
  - 终态不可再迁移（`_ALLOWED_TRANSITIONS`）。
- `SUCCEEDED` 强制要求 `eval`（且 primary 有限）、`per_sample` 证据；非 `SUCCEEDED` 不能携带 eval/verdict；`FAILED` 必须带 error。

### 1.3 关键不变量

- 假设 `id` 与 `order` 唯一；`order` 缺省时自动分配为当前最大值 + 1。
- `parent_id` 指向的父实验必须已存在。
- `supersedes` 中的每个假设 id 必须在该 hypothesis 的父实验祖先链上，且不能包含自己、不能重复（`_validate_supersedes`）。
- `add_experiment` 校验：实验 id 非空且唯一；hypothesis 已知；hypothesis 未绑定其他实验；父实验存在；实验 parent 与假设 parent 一致。
- `from_dict` 重建时逐条校验版本（v2/v3）、顶层字段、hypothesis id 与 key 一致、experiment eval id 与 key 一致、父子关系无环、SOTA 合法性。

### 1.4 主要 API

| API | 作用 |
|---|---|
| `add_hypothesis` / `get_hypothesis` / `hypotheses` / `pending_hypotheses` | 登记 / 读取 / 全部假设 / 仅 `PROPOSED` 假设 |
| `update_hypothesis_status` | 原地更新假设状态 |
| `add_experiment` / `get_experiment` / `experiments(kind)` | 登记 / 读取 / 按 plan kind 过滤实验 |
| `root_experiment_ids` / `list_children` / `list_descendants` | 树的根 / 直接子 / BFS 全部后代 |
| `experiment_path` / `hypotheses_path` | 从根到某实验的祖先链；`_parent_chain` 有环检测 |
| `experiment_for_hypothesis` | hypothesis → experiment id（最多一个） |
| `active_hypotheses` | 父实验祖先链减去 `supersedes` 的假设 + 子假设 |
| `transition_experiment` | 按状态机推进实验状态 |
| `complete_experiment` | `RUNNING → SUCCEEDED`，写入 eval / verdict / artifacts / commit |
| `attach_artifact` | 附加产物引用 |
| `set_sota` / `best_experiment_id` | 设置 / 读取 SOTA；SOTA 只能是成功的 baseline 或 search 实验 |
| `save` / `load` / `to_dict` / `from_dict` | 原子 JSON 持久化与重建（版本字段 `SAVE_VERSION = 3`） |

### 1.5 GUI 只读图算法

文件：`src/athena/gui/graph.py`

- `build_hypothesis_graph`：把树投影为 hypothesis 图，两类边：`lineage`（父→子）和 `supersedes`（子→被取代的祖先）。
- 还提供 `topological_order`、`cycle_detect`、`best_path`、`rank_pending`、`supersedes_closure`、`refutation_reachability` 等只读算法，供前端可视化。

---

## 2. SEARCH 搜索机制

> 当前 `main` 中已没有独立的 `src/athena/research/search.py`（历史 `SearchService` 已在
> `d8282c5` 中删除）。滚动搜索的实现收敛在 `src/athena/research/supervisor/` 包中。

### 2.1 参与模块

| 文件 | 职责 |
|---|---|
| `supervisor/supervisor.py` | `Supervisor`：唯一写者，运行 `run_search` 调度循环，创建/结算 Plan，维护 SOTA |
| `supervisor/scheduler.py` | `Scheduler`：纯确定性的槽位填充，输出 `ScheduleAction`；`count_search_attempts` |
| `supervisor/policy.py` | `HypothesisPolicy` 协议 + `EloPolicy`（单边 Elo）；`queue_order` |
| `supervisor/ranker.py` | `Selector`：UCB 风格排序 + Jaccard 去重 |
| `supervisor/recovery.py` | `Recovery`：崩溃恢复，协调 `state.json` 与 `research_tree.json` |
| `supervisor/state.py` | `ResearchState`：`status` / `phase` / `search_limit` / `concurrency` / `ideator_count` / `hypotheses_per_ideator` / `manual_mode` / `plans` / `eda_dir` |
| `supervisor/plans.py` | `PlanState` / `PlanInput` / `PlanBest` / `PlanDecision` |
| `supervisor/experiment.py` | Plan 执行、可信打分、patience 结算（`PlanRunner`、`apply_trusted_score`、`decide_settlement`、`load_best`） |

### 2.2 SEARCH 调度循环

文件：`supervisor/supervisor.py` → `Supervisor.run_search`

1. `_fill_slots()` 向 `Scheduler.next_actions` 要一组动作。
2. 若有动作且当前没有 running plan，且产出了新候选，则继续填槽。
3. 若没有 running plan 且 manual 模式等待人工选择，`_wait_for_manual_selection` 阻塞到 `select_next_hypothesis`。
4. 否则等待任意一个 Plan turn 完成（`asyncio.wait(FIRST_COMPLETED)`），取出完成的 task，调用 `_apply_completed_turn`。
5. 循环直到 `_stopped` 或 SEARCH 自然收敛。

### 2.3 `Scheduler.next_actions` 的槽位填充顺序

文件：`supervisor/scheduler.py`

在 `state.phase == "SEARCH"` 时，按固定优先级填满 `free_slots = concurrency - len(running)`：

1. **RESUME**：已存在、未运行、且 `turns_used < turn_limit` 的 SEARCH Plan 先恢复。
2. **START_NEXT_HYPOTHESIS**：人工点名的单个假设（`human_next` / `select_next_hypothesis`）一次性绕过优先级队列，但不修改优先级。
3. **START_NEW**（自动模式）：对 `pending_hypotheses()` 中未执行、未在 `state.plans`、非 `human_next` 的候选，先 `Selector.deduplicate`（只作用于本轮选择，不删除图中假设），再 `Selector.rank` 排序，按序创建并启动 Plan。
4. **GENERATE**：剩余空槽且还有 search 预算时，请求 SupervisorAgent / Ideator 生成新假设。

手动模式（`manual_mode=True`）下跳过第 3 步，只在没有 pending 候选时请求生成。

### 2.4 搜索预算与并发

- `count_search_attempts(state, tree)` = 已终态（`SUCCEEDED/FAILED/CANCELLED`）的 `search` 实验数 + 仍在 `state.plans` 中的 SEARCH Plan 数。
- `create_budget = search_limit - count_search_attempts(...)`，控制还能创建多少个新 Plan。
- `state.concurrency` 限制同时 running 的 Plan 数。

### 2.5 Plan 生命周期

1. **创建（`start_plan`）**：冻结 `PlanInput`（hypothesis、父实验与祖先假设、reference metric / priority、方向、tolerance、evaluator、tree snapshot、human guidance、初始 turn/patience），存为 artifact；从参考实验 commit 创建 worktree；在树中登记 `exp_{hypothesis_id}` 并置 `RUNNING`；写入 `PlanState`；`resume_agent` 拉起 plan agent。
2. **回合（`_run_one_turn`）**：先将 `turns_used + 1` 持久化（防 crash），再向 Plan Agent 发 followup；解出 `PlanDecision`（continue/submit/abandon）。
   - abandon 且无 best：直接结算失败。
   - 否则执行 `run_plan_turn`（manifest 执行 + 可信打分，见 `supervisor/experiment.py`）。
3. **结算决策（`decide_settlement`）**：
   - `submit` / `abandon` → settle（abandon 且无 best 按无证据结算）。
   - `continue` 且 `stale_rounds >= patience` → settle（历史 best）。
   - `continue` 且 `turn_limit` 用完：有 best → settle；无 best → wait（进入 `WAITING`，等待人工补充预算）。
   - 否则 continue。
4. **结算（`_settle_plan`）**：
   - 无 best：实验 `FAILED`，假设状态 `INCONCLUSIVE`，不更新优先级。
   - 有 best：比较 `best.metric` 与 `reference_metric`，按 `direction` 和 `tolerance` 判定 `WIN / DRAW / LOSS`；实验 `SUCCEEDED`；用 `Scheduler.settle` 更新优先级；假设状态 `SUPPORTED`（WIN）或 `REFUTED`（DRAW/LOSS）。
   - 若 `best.metric` 优于当前 SOTA（同方向 + tolerance），则 `set_sota(exp_id)`。
   - 持久化树，移除 `state.plans[plan_id]`。

### 2.6 指标比较与评级策略

- `_compare_metric`：`maximize` 时 `delta = candidate - reference`；`minimize` 时 `delta = reference - candidate`；`delta > tolerance` → WIN，`delta < -tolerance` → LOSS，否则 DRAW。
- `EloPolicy`（`supervisor/policy.py`）：
  - `seed(parent)`：新假设继承父假设 priority（无父则 1000.0）。
  - `settle(reference_priority, outcome)`：`reference_priority + k * (score - 0.5)`，score 为 WIN=1.0 / DRAW=0.5 / LOSS=0.0，默认 `k=32`。
- `Scheduler` 默认使用 `EloPolicy` 和 `Selector(EloPolicy)`。

### 2.7 恢复机制

文件：`supervisor/recovery.py` + `Supervisor.recover`

- 若 `.athena/research_tree.json` 存在，先加载树。
- 对 `state.plans` 逐项检查 `context_ref` artifact 是否还在、`PlanInput.reference_experiment_id` 对应 worktree 是否可重建。
- `Recovery.reconcile` 决定：保留可恢复 Plan、结算已写入树的 Plan、缺失 context/workspace 时置 `WAITING` 而不重新发明资源。
- 重启后稳定恢复 agent context 与 worktree 路径（见 `test/integration/research/test_search_recovery.py`）。

---

## 3. IdeaGenerator 机制

当前系统有两条 IdeaGenerator 路径：

### 3.1 路径 A：SEARCH 内的实时 EDA-grounded Ideator（线上主路径）

文件：`src/athena/research/agent_turn_runner.py`、`src/athena/agents/ideator_agent.py`、
`src/athena/core/agent/prompts/ideator_agent.md`、`ideator_gated_agent.md`

流程：

1. `Scheduler` 发出 `GENERATE` 动作，`Supervisor._fill_slots` 调用 `AgentTurnRunner.run_ideator_turn(count)`。
2. `run_ideator_turn`：
   - 解析并校验 `state.eda_dir` 指向的 EDA 目录（必须位于项目根内）。
   - 注册 Ideator Agent（绑定 EDA workspace、执行运行时、Kaggle 工具等）。
   - 计算批大小：`batch = max(count, ideator_count * hypotheses_per_ideator)`。
   - `_ideator_allocations` 把 batch 分配到最多 `ideator_count` 个 lane。
   - 并发跑 `_run_ideator_lane`；每轮使用唯一前缀 `ideator-{round}-{index}`，避免前端把新一轮追加到旧 lane。
3. `_run_ideator_lane`：
   - 创建 root agent，prompt 要求：先读 `RESEARCH_HANDOFF.md`，在 EDA 工作区只读探索（读文件 / 跑命令），提出 1–5 条可证伪假设。
   - 如果 evaluator 有 `HANDOFF.md`，作为 context 附带。
   - 输出契约：
     - **gated 模式**：`IdeatorHypothesisBatch`（富结构：premises / inference_chain / predictions / disconfirmers + 可选 eda_request）。
     - **baseline 消融模式**：`HypothesisBatch`（statement / intervention / expected_effect + 可选 eda_request）。
   - gated 模式下 `_finish_ideator_batch` 调 `run_light_pipeline`；门禁全拒时最多重试 `MAX_GATE_RETRIES = 2` 次，重试 prompt 会携带逐条拒绝理由，要求生成侧换实质方向而不是改写重述。
4. 汇总各 lane 的 `HypothesisBatch`：合并 `hypotheses`，并收集所有非空 `eda_request` 触发动态 EDA（见第 5 节）。
5. 返回全部 hypotheses 给 `Supervisor.register_hypotheses` 写入 ResearchTree。

### 3.2 路径 B：离线全流程 Idea Generation（论文挖掘 / 批量候选场景）

文件：`src/athena/research/idea_generation/workflow.py` → `run_full_pipeline`

入口：`run_full_pipeline(problem, gap_miner_agent, novelty_agent, domain_review_agent, artifacts, corpus_ref, ...)`

步骤：

1. **[2] 空白挖掘**：`mine_research_gaps` 产出一组研究空白 `GapCandidate`。
2. **[3] 多候选生成 + 去重**：`generate_candidates` 并行跑最多 `MAX_VERBALIZED_SAMPLES = 5` 个策略 Agent：
   - `analogical_transfer`（类比迁移）
   - `mechanistic_reasoning`（机制推演）
   - `counterintuitive`（反直觉假设）
   - `constraint_relaxation`（约束松弛）
   - `boundary_extrapolation`（边界外推）
   - 每个策略单轮产出一个 `HypothesisPackage`；`deduplicate_candidates` 按 `novel_hypothesis` 词集 Jaccard ≥ 0.8 去重，同组保留 `sampling_probability` 最高者。
3. **[4]-[8] 每个候选并发过闸**：`_process_one_candidate`
   - [4] `structural_check` + `falsifiability_check` → `pre_gate`（不合格直接淘汰，跳过后续昂贵步骤）。
   - [5] 新颖性审计：`collect_novelty_evidence`（失败降级为空报告）。
   - [6] 三视角审阅委员会：`review_board`。
   - [7] 验证方案：`match_verifier` + `plan_validation`。
   - [8] `hard_gate`；若判 `REVISE` 且 `is_revisable`，进入 [8'] 修订闭环：`run_debate` → `refresh_stale_evidence` → 再次 `hard_gate`。
4. **[9] Pairwise Elo 排序**：对存活候选（PASS/EXPLORATORY）两两匿名双向比较，`HypoPriList` 排名（见第 4 节）。
5. 返回 `(results, ranking)`；`hypothesis_selector.select_next_hypotheses` 按预算截断。

### 3.3 实时 Ideator 的门禁：`run_light_pipeline`

文件：`src/athena/research/idea_generation/gate.py`

实时 Ideator 不会跑完整 workflow，而是走一个轻量门禁：

- 对每个 `IdeatorHypothesisDraft` 构造 `HypothesisPackage`（`generation_strategy="eda_grounded"`）。
- `pre_gate`：结构完整性 + 可证伪性。
- 审阅：无语料时只跑 `methodology + statistics` 两个视角；有 `corpus_ref` 时先跑新颖性审计，再跑全部三个视角。
- `light_hard_gate`（无语料）或 `hard_gate`（有语料）。
- REVISE/REJECT 的候选本轮直接丢弃（不进入修订闭环），但逐条理由返回给生成侧。
- 存活候选若 > 1，匿名双向 pairwise Elo 排名，按名次转回 `core.Hypothesis`。
- 语料半配置（给了 `corpus_ref` 没给 agent）直接抛错，绝不静默降级。

### 3.4 门禁判定逻辑

文件：`src/athena/research/idea_generation/gatekeeper.py`

`pre_gate` 两项 rubric：

- `evidence_traceable`：所有 premises 绑证据 + 新假设带 predictions/disconfirmers。
- `falsifiable`：LLM 判定存在可检验推论，且无不可观测变量。

`hard_gate`（完整）在 pre_gate 之上追加：

- `novelty_ok`：`facet_overlap` 非空且均值 < 0.7，否则 REVISE / REJECT。
- 每视角 `risk_ok_{perspective}`：`failed=False`、`fatal_flaw_found=False`、`unaddressed_risks ≤ MAX_TOLERATED_RISKS(6)`。
- `risk_total`：跨视角总风险 ≤ `max_total_risks(N) = 6N - 1`。
- `verifier_ok`：有匹配的 verifier；无 verifier → `EXPLORATORY`。

`light_hard_gate` 跳过 `novelty_ok` 与 domain_consistency，其余优先级一致。

判定优先级：结构/可证伪性 → 新颖性缺证据/超阈值 → 视角 fatal flaw → 视角风险超阈值/失败 → 总量超阈值 → verifier 缺失 → PASS。

---

## 4. Hypothesis 排序机制

系统中存在两层排序，分别服务于不同阶段。

### 4.1 层 1：SEARCH 调度排序（决定先跑哪个假设）

文件：`src/athena/research/supervisor/ranker.py`、`policy.py`、`scheduler.py`

`Selector.rank(tree, candidates)` 对 pending 候选计算确定性分数：

```
score(h) = prior_weight * rubric_prior(h)
         + strength_weight * normalized_strength(h)
         + novelty_weight * novelty(h)
         - cost_weight * min(max(h.cost, 0), 1)
```

- `rubric_prior`（冷启动先验，[0.4, 1.0]）：有 `sources` +0.3，intervention 词元数 ≥ 3 +0.3。
- `normalized_strength`（强度）：把 `policy.priority` 归一化到 [0,1]（Elo/Bradley-Terry proxy，来自真实 WIN/DRAW/LOSS 结果）。
- `novelty`（UCB 探索项）：`1 - max(Jaccard(statement+intervention, 已结算假设))`；没有已结算假设时为 1.0。
- `cost`：归一化成本惩罚。

`RankConfig` 默认权重：`prior=0.4, strength=0.3, novelty=0.2, cost=0.1, dedup_threshold=0.8`。

`Selector.deduplicate(candidates, existing)`：

- 对 `statement + intervention` 做小写词元 Jaccard 相似度，≥ 0.8 视为重复。
- 与图中已有假设及本轮已保留候选比较，贪心保序（先到先得）。

`Scheduler._queued` 在排序前先过滤掉：

- 已经有 experiment 的假设；
- 已在 `state.plans` 中的假设；
- `human_next` 命中的假设（单独走人工槽位）。

排序键：`(-score, order)`，即分数降序、同分按 `order` FIFO。

`EloPolicy` 的评级更新：

```
priority' = reference_priority + k * (score - 0.5)
score = WIN 1.0 / DRAW 0.5 / LOSS 0.0
```

新假设通过 `seed(parent)` 继承父假设 priority；没有父则 1000.0。结算时用**冻结的 reference priority**，不受期间其它结算影响。

### 4.2 层 2：Idea Generation 候选排序（决定候选质量顺序）

文件：`src/athena/research/idea_generation/ranking.py`、`workflow.py`

`HypoPriList` 是在线 Elo 优先队列：

- 初始 rating 1200，K=32。
- `record_comparison(PairwiseComparison)`：标准 Elo 公式即时更新双方 rating，并把 rationale 存为该候选的证据。
- `rank()`：rating 降序，tie-break 为 `idea_id` 升序，输出 `RankedCandidate(rating, comparisons, evidence)`。

`workflow._pairwise_compare` 的偏置控制：

- 两个候选匿名化后**双向各评一次**（A vs B 和 B vs A）。
- 双向一致才认为 winner 可靠；不一致则记录 disagreement（可能存在 position bias）。
- 所有配对 `asyncio.gather` 并发。

`gate._pairwise_compare`（实时 Ideator 门禁内部）使用同样模式。

`hypothesis_selector.select_next_hypotheses(results, ranking, budget)`：

- 按 rating 降序取前 `budget` 个，交叉引用出完整审计记录；
- budget ≤ 0 返回空；ranking 引用了 results 不存在的 idea_id 时抛 `KeyError`（调用方传错 pair 要显式失败）。

### 4.3 两者关系

- **层 2** 决定“Ideator 产出的候选按质量怎么排”，产出 `Hypothesis` 列表。
- **层 1** 决定“进入 ResearchTree 后，在 SEARCH 并发槽位中先执行谁”，以及“实验结算后假设的优先级如何演化”。
- 两者不共享状态：层 2 是生成侧的横向比较；层 1 是搜索侧基于真实实验反馈的纵向演化。

---

## 5. 动态 EDA 机制

### 5.1 PREPARE 固定 EDA 工作区

文件：`src/athena/research/phase_runner.py`、`src/athena/research/runtime.py`

1. PREPARE 开始时，`LocalGitWorkspace.create(base_commit, "athena/prepare", name="eda")` 创建**固定名称** `eda` worktree。
2. `run_prepare_phase` 把 EDA 目录相对项目根的路径写入 `rt._state.eda_dir`（例如 `workspaces/eda`），随 `state.json` 持久化。
3. Evaluator Agent 在 `workspaces/evaluator/` 写评估器并冻结；Prepare Agent 在 EDA worktree 写 EDA 报告、baseline、`RESEARCH_HANDOFF.md`，并产出可信 baseline 分数。
4. `Supervisor._run_prepare` 将 baseline 写入 ResearchTree 并置为 SOTA。

### 5.2 断点续传保护

文件：`src/athena/research/runtime.py`（构造期）

- 恢复 `state.json` 时，如果 `eda_dir` 指向项目根之外（例如跨目录拷贝的旧 state），强制置空，让 PREPARE 重建。

### 5.3 Ideator 发起动态 EDA 请求

文件：`src/athena/core/research_models.py`（`HypothesisBatch.eda_request`）、
`src/athena/research/idea_generation/idea_schemas.py`（`IdeatorHypothesisBatch.eda_request`）、
`agent_turn_runner.py`

1. 普通（baseline）Ideator 输出 `HypothesisBatch`、gated Ideator 输出 `IdeatorHypothesisBatch`，两者都带可选字段 `eda_request`：
   - 非空：表示“现有 EDA 不足以支撑可靠假设，请求补充分析”。
   - 为 null/空：不需要补充。
2. `_finish_ideator_batch` 在两条路径（baseline 直接入库 / gated 过门禁）都保留 `eda_request`。
3. `AgentTurnRunner.run_ideator_turn` 汇总各 lane 的 `eda_request`，合并成一个多行请求。
4. 若存在请求，调用 `run_data_turn`。

### 5.4 Data Agent 执行补充分析

文件：`src/athena/agents/data_agent.py`、`src/athena/core/agent/prompts/data_agent.md`、
`agent_turn_runner.run_data_turn`

1. `run_data_turn` 注册 Data Agent（agent type/id 均为 `data`），工具绑定 EDA 工作区。
2. Prompt 约束 Data Agent：
   - 先读现有 EDA report 和 `RESEARCH_HANDOFF.md`，不要重复已有分析。
   - 写 `eda_extra.py` 脚本，用 `shell_command` 跑通（只能 import `numpy/pandas/matplotlib`）。
   - 新图保存到 `figures/`。
   - **只追加**新 section 到现有 Markdown report（`## Additional EDA: <request>`），不覆盖既有文件。
   - 禁止修改原始数据、evaluator、baseline、`predictions/`、`experiment.json`。
3. 输出 `EdaResult`（一句话 summary），通过 `publish_output` 发布“补充 EDA 完成”。
4. 后续 Ideator 回合再次读取 EDA 目录时，会看到追加后的报告和图片，从而在更丰富证据上提出假设。

### 5.5 动态 EDA 的闭环

```
SEARCH 空槽 → Ideator 探索 EDA 目录 → 输出假设 + eda_request
    → AgentTurnRunner 合并请求 → Data Agent 写回 EDA 目录（追加 report/figures）
    → 下一轮 Ideator 读取更新后的 EDA → 提出更可靠的假设
```

---

## 6. 关键文件索引

| 主题 | 文件 |
|---|---|
| 研究树模型 | `src/athena/core/research_tree.py` |
| 核心研究模型 | `src/athena/core/research_models.py` |
| SEARCH 调度循环 | `src/athena/research/supervisor/supervisor.py` |
| SEARCH 调度器 | `src/athena/research/supervisor/scheduler.py` |
| 评级策略 | `src/athena/research/supervisor/policy.py` |
| 排序与去重 | `src/athena/research/supervisor/ranker.py` |
| Plan 状态/输入 | `src/athena/research/supervisor/plans.py` |
| Plan 执行与结算 | `src/athena/research/supervisor/experiment.py` |
| 崩溃恢复 | `src/athena/research/supervisor/recovery.py` |
| Idea Generation 全流程 | `src/athena/research/idea_generation/workflow.py` |
| 候选生成与去重 | `src/athena/research/idea_generation/candidate_generation.py` |
| 门禁判定 | `src/athena/research/idea_generation/gatekeeper.py` |
| 实时 Ideator 门禁 | `src/athena/research/idea_generation/gate.py` |
| Pairwise Elo 排序 | `src/athena/research/idea_generation/ranking.py` |
| 候选选择 | `src/athena/research/idea_generation/hypothesis_selector.py` |
| Ideator 注册 | `src/athena/agents/ideator_agent.py` |
| Data Agent 注册 | `src/athena/agents/data_agent.py` |
| Agent turn 编排 | `src/athena/research/agent_turn_runner.py` |
| PREPARE/EDA 目录创建 | `src/athena/research/phase_runner.py` |
| 组合根 | `src/athena/research/runtime.py` |
| GUI 图算法 | `src/athena/gui/graph.py` |
| Ideator 提示词 | `src/athena/core/agent/prompts/ideator_agent.md`、`ideator_gated_agent.md` |
| Data Agent 提示词 | `src/athena/core/agent/prompts/data_agent.md` |

## 7. 相关测试

- `test/unit/research/test_research_tree_scheduling.py`
- `test/integration/research/test_rolling_search.py`
- `test/integration/research/test_search_recovery.py`
- `test/unit/research/supervisor/test_scheduler.py`
- `test/unit/research/supervisor/test_ranker.py`
- `test/unit/research/supervisor/test_policy.py`
- `test/unit/idea_generation/`（门禁、轻量流水线、消融开关等）

## 8. 关联设计文档

- [Hypothesis 本地池设计](autoresearch/2026-08-15-hypothesis-local-pool-design.md)：独立 `HypothesisPool` 服务与 `hypothesis_pool.json`，是 AutoResearch 从 idea 到 paper 的候选生命周期索引。
- [AutoResearch 框架与 TS 插件设计](autoresearch/2026-08-15-autoresearch-ts-plugin-design.md)：端到端 `Idea Generation → Experiment（Athena 子集）→ Paper Writing → Refinement → Packaging` 的概念设计。

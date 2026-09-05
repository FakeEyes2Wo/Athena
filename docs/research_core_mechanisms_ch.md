# Athena 研究核心机制整理：ResearchTree / PREPARE / SEARCH / IdeaGenerator / Hypothesis 排序 / 动态 EDA

> 状态：current
> 覆盖范围：`src/athena/core/research_tree.py`、`src/athena/core/research_models.py`、
> `src/athena/research/supervisor/`、`src/athena/research/idea_generation/`、
> `src/athena/research/turns/`、`src/athena/research/runtime/phase_runner.py`、
> `src/athena/research/runtime/facade.py`、`src/athena/research/prepare/`、
> `src/athena/agents/ideator_agent.py`、`src/athena/agents/task_agents.py`。
> 本文整理的是当前 `main` 分支的真实实现，不描述已删除或未接线的历史代码。

---

## 0. 全景：一次研究运行的生命周期

```
PREPARE ──> SEARCH ──> VALIDATE ──> COMPLETED
  │            │
  │            ├─ 动态 EDA：Ideator 提出 eda_request → Data Agent 补分析写回 EDA 目录
  │            └─ IdeaGenerator（Ideator Agent）产生 Hypothesis → 门禁过滤 → 入 ResearchTree → 排序调度
  │
  └─ 在固定名称 worktree（name="eda"）里做 EDA，冻结 evaluator，完成 baseline 研究与来源验证，
     经来源研究/独立验证门禁后由 Prepare Agent 实现 baseline，可信评分后写入 ResearchTree 作为 SOTA
```

- 组合根：`src/athena/research/runtime/facade.py` 的 `ResearchRuntime`。
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

> 当前 `main` 中已没有独立的 SearchService 模块（历史实现已在
> `d8282c5` 中删除）。滚动搜索的实现收敛在 `src/athena/research/supervisor/` 包中。

### 2.1 参与模块

| 文件 | 职责 |
|---|---|
| `supervisor/supervisor.py` | `Supervisor`：唯一写者，运行 `run_search` 调度循环，创建/结算 Plan，维护 SOTA |
| `supervisor/scheduling.py` | `Scheduler`、`EloPolicy` 与 `Selector`：调度、评级、排序和去重 |
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

文件：`supervisor/scheduling.py`

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
- `EloPolicy`（`supervisor/scheduling.py`）：
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

文件：`src/athena/research/turns/ideator.py`、`src/athena/agents/ideator_agent.py`、
`src/athena/agents/prompts/ideator_agent.md`、`src/athena/agents/prompts/ideator_gated_agent.md`

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

### 3.2 单一 Idea Generation 路径

当前生产实现只有实时 Ideator 门禁路径。历史离线 `workflow.py`、候选生成器和
局部排序器已删除；论文证据由 `literature/` 提供，所有通过门禁的候选按提交顺序
进入统一的 ResearchTree/Supervisor 假设池。

### 3.3 实时 Ideator 的门禁：`run_light_pipeline`

文件：`src/athena/research/idea_generation/gate.py`

实时 Ideator 不会跑完整 workflow，而是走一个轻量门禁：

- 对每个 `IdeatorHypothesisDraft` 分配内部 idea id，draft 本身直接贯穿门禁。
- `pre_gate`：证据绑定 + 可证伪性。
- 审阅：并行运行 `methodology + statistics` 两个独立视角。
- `light_hard_gate`：综合可证伪性、单视角风险和跨视角风险总量。
- REVISE/REJECT 的候选本轮直接丢弃（不进入修订闭环），但逐条理由返回给生成侧。
- 存活候选按提交顺序转回 `core.Hypothesis`，不在门禁内部维护第二套排序状态。

### 3.4 门禁判定逻辑

文件：`src/athena/research/idea_generation/gatekeeper.py`

`pre_gate` 两项 rubric：

- `evidence_traceable`：所有 premises 绑证据 + 新假设带 predictions/disconfirmers。
- `falsifiable`：LLM 判定存在可检验推论，且无不可观测变量。

`light_hard_gate` 在 pre_gate 之上追加：

- 每视角 `risk_ok_{perspective}`：`failed=False`、`fatal_flaw_found=False`、`unaddressed_risks ≤ MAX_TOLERATED_RISKS(6)`。
- `risk_total`：跨视角总风险 ≤ `max_total_risks(N) = 6N - 1`。

判定优先级：证据/可证伪性 → 视角 fatal flaw → 视角风险超阈值/失败 → 总量超阈值 → PASS。

---

## 4. Hypothesis 排序机制

系统只保留一套由 Supervisor 拥有的排序，避免生成侧和执行侧各自维护优先级。

### 4.1 层 1：SEARCH 调度排序（决定先跑哪个假设）

文件：`src/athena/research/supervisor/scheduling.py`

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

### 4.2 Idea Generation 与调度的边界

Idea Generation 只负责产出和门禁，不维护局部 Elo 或候选池。候选进入
ResearchTree 后，统一由 `supervisor/scheduling.py` 去重、排序和按实验结果更新优先级。

---

## 5. PREPARE 证据门禁与动态 EDA 机制

### 5.1 PREPARE 固定 EDA 工作区

文件：`src/athena/research/runtime/phase_runner.py`、`src/athena/research/runtime/facade.py`

1. PREPARE 开始时，`LocalGitWorkspace.create(base_commit, "athena/prepare", name="eda")` 创建**固定名称** `eda` worktree。
2. `run_prepare_phase` 把 EDA 目录相对项目根的路径写入 `rt._state.eda_dir`（例如 `workspaces/eda`），随 `state.json` 持久化。
3. Evaluator Agent 在 `workspaces/evaluator/` 写评估器并冻结；EDA 完成后，baseline Ideator 先做 Web/论文研究，写入 `BASELINE_RESEARCH.json` 与 `BASELINE_DESIGN.md`。
4. 平台独立执行 Git-first 来源验证：优先校验公开 HTTPS Git；无合格仓库时，只接受标题匹配且 OpenAlex `cited_by_count >= 100` 的论文例外。通过后写入 `BASELINE_RESEARCH_VERIFICATION.json`。
5. Prepare Agent 只在验证通过后注册，它读取三份 baseline 产物，实现已验证的方法，写 baseline 与 `RESEARCH_HANDOFF.md`，再由可信 evaluator 评分。
6. `Supervisor._run_prepare` 将 baseline 写入 ResearchTree 并置为 SOTA。

来源验证只证明仓库可访问到某个 revision，不代表仓库安全或适合执行。验证器使用浅层、无 checkout 的 clone，绝不 checkout、导入、安装、复制或运行第三方仓库代码。

### 5.2 Baseline 研究与来源验证门禁

文件：`src/athena/research/prepare/baseline_research.py`、
`src/athena/research/prepare/source_verification.py`、
`src/athena/research/prepare/baseline.py`、`src/athena/research/prepare/orchestrator.py`

```text
EDA_HANDOFF.md
  -> baseline Web/论文研究
  -> BASELINE_RESEARCH.json + BASELINE_DESIGN.md
  -> 平台 Git/OpenAlex 验证
  -> BASELINE_RESEARCH_VERIFICATION.json
  -> Prepare Agent 实现
  -> 可信评分
```

- `BASELINE_RESEARCH.json` 记录搜索、候选取舍、数据模态/任务及训练策略证据；`BASELINE_DESIGN.md` 保留人可读的选型与本地适配边界。数据充足性是模态相关判断，不使用全局固定样本数阈值；`unknown` 不允许从零训练，从零训练还必须同时有本地 EDA/计算和可比规模来源证据。
- 验证顺序是 Git-first，OpenAlex 100 引用是无合格仓库时的独立例外。Git 路线仅允许无凭据的公开 HTTPS URL，在临时目录使用 `--no-checkout` 浅克隆并锁定 `HEAD`。“可克隆”只证明当时可公开获取，不证明仓库安全；资格校验不 checkout、导入、安装、复制或执行任何第三方代码。
- 第一次产物失败时，同一 `baseline_ideator` 收到一次结构化修复机会，必须重写两份完整产物；第二次仍失败则以 `BaselineResearchError` 终止 PREPARE，Prepare Agent 不会注册。
- 三份 durable 产物位于 EDA worktree 根目录。断点恢复时，只有当验证文件的 schema、候选 ID 和 `BASELINE_RESEARCH.json` 的 SHA-256 digest 都匹配时才复用；否则重新校验/验证。

### 5.3 断点续传保护

文件：`src/athena/research/runtime/facade.py`（构造期）

- 恢复 `state.json` 时，如果 `eda_dir` 指向项目根之外（例如跨目录拷贝的旧 state），强制置空，让 PREPARE 重建。

### 5.4 Ideator 发起动态 EDA 请求

文件：`src/athena/core/research_models.py`（`HypothesisBatch.eda_request`）、
`src/athena/research/idea_generation/idea_schemas.py`（`IdeatorHypothesisBatch.eda_request`）、
`turns/ideator.py`

1. 普通（baseline）Ideator 输出 `HypothesisBatch`、gated Ideator 输出 `IdeatorHypothesisBatch`，两者都带可选字段 `eda_request`：
   - 非空：表示“现有 EDA 不足以支撑可靠假设，请求补充分析”。
   - 为 null/空：不需要补充。
2. `_finish_ideator_batch` 在两条路径（baseline 直接入库 / gated 过门禁）都保留 `eda_request`。
3. `AgentTurnRunner.run_ideator_turn` 汇总各 lane 的 `eda_request`，合并成一个多行请求。
4. 若存在请求，调用 `run_data_turn`。

### 5.5 Data Agent 执行补充分析

文件：`src/athena/agents/task_agents.py`、`src/athena/agents/prompts/data_agent.md`、
`turns/runner.py::AgentTurnRunner.run_data_turn`

1. `run_data_turn` 注册 Data Agent（agent type/id 均为 `data`），工具绑定 EDA 工作区。
2. Prompt 约束 Data Agent：
   - 先读现有 EDA report 和 `RESEARCH_HANDOFF.md`，不要重复已有分析。
   - 写 `eda_extra.py` 脚本，用 `shell_command` 跑通（只能 import `numpy/pandas/matplotlib`）。
   - 新图保存到 `figures/`。
   - **只追加**新 section 到现有 Markdown report（`## Additional EDA: <request>`），不覆盖既有文件。
   - 禁止修改原始数据、evaluator、baseline、`predictions/`、`experiment.json`。
3. 输出 `EdaResult`（一句话 summary），通过 `publish_output` 发布“补充 EDA 完成”。
4. 后续 Ideator 回合再次读取 EDA 目录时，会看到追加后的报告和图片，从而在更丰富证据上提出假设。

### 5.6 动态 EDA 的闭环

```
SEARCH 空槽 → Ideator 探索 EDA 目录 → 输出假设 + eda_request
    → AgentTurnRunner 合并请求 → Data Agent 写回 EDA 目录（追加 report/figures）
    → 下一轮 Ideator 读取更新后的 EDA → 提出更可靠的假设
```

### 5.6 Baseline 研究与来源验证门禁

Baseline 是 PREPARE 的独立证据链，不与 SEARCH 的假设生成混用：

1. `baseline_ideator` 先读取 `EDA_HANDOFF.md`、数据与 evaluator 合同，并用
   网页/论文工具检索至少两个查询。候选可以来自论文、官方实现或相关已完赛
   Kaggle 方案，但 Kaggle write-up 只是辅助证据；选中项仍需绑定可验证的公开
   Git 仓库或论文定位符。
2. `BASELINE_RESEARCH.json` 保存数据模态、有效训练单元、数据规模判断、候选与
   决策；`BASELINE_DESIGN.md` 保存可实现的主方案，并用精确 marker 重复候选 ID
   与训练策略。数据规模判断是 modality-aware 的，不使用全局样本数阈值；
   未知或不足的数据 regime 不得选择从零训练。
3. 平台先尝试公开 HTTPS Git。验证只使用 `--depth 1 --filter=blob:none
   --no-checkout`，关闭交互凭据并禁止非 HTTPS 协议；失败后才尝试 OpenAlex。
   OpenAlex 必须返回标题匹配且 `cited_by_count >= 100` 的 work，Agent 自报的
   引用数不作为依据。
4. 成功后写入 `BASELINE_RESEARCH_VERIFICATION.json`，其中绑定 research 文件的
   SHA-256、候选 ID、验证路线和 revision/authority 元数据。恢复时仅复用 digest
   与候选一致的验证结果，研究文件变化则重新验证。
5. 缺文件、格式错误或来源不合格允许一次 repair turn。第二次仍失败时，PREPARE
   终止并报告诊断，不使用 task-only fallback，也不提前注册 Prepare Agent。

三个持久化文件的职责如下：

| 文件 | 责任 | 内容 |
|---|---|---|
| `BASELINE_RESEARCH.json` | baseline_ideator | 候选来源、数据评估、搜索记录和选择决策 |
| `BASELINE_DESIGN.md` | baseline_ideator | 与 JSON 一致的架构、训练策略和实现说明 |
| `BASELINE_RESEARCH_VERIFICATION.json` | 平台 | digest 绑定的 Git/OpenAlex 独立验证证据 |

---

## Evaluator contract v2, validation, and durable run documents

SEARCH and FINAL are frozen with evaluator contract v2. Each `metric.json`
declares the same `task_type`, `primary_metric`, and complete ordered
`class_labels`; classification scoring keeps that full label universe, including
classes absent from a particular partition. Both evaluators use the same
label-free identity algorithm and reject missing, duplicate, or unexpected IDs.

VALIDATE is optional in the TUI and is disabled by default. Passing `--validate`
enables automatic continuation after SEARCH; otherwise the run can stop for
manual review or an explicit VALIDATE command.

Each baseline, SEARCH, and FINAL settlement writes one JSON record below
`.athena/exp_docs/runs/`. The platform also refreshes
`.athena/exp_docs/FINAL_REPORT.md` and `.athena/exp_docs/OPTIMIZATION.md` after
each stage. These documents are the durable audit trail for scores, artifacts,
provenance, and deterministic failure causes, and are reused when a run resumes.

## 6. 关键文件索引

| 主题 | 文件 |
|---|---|
| 研究树模型 | `src/athena/core/research_tree.py` |
| 核心研究模型 | `src/athena/core/research_models.py` |
| SEARCH 调度循环 | `src/athena/research/supervisor/supervisor.py` |
| SEARCH 调度、评级、排序与去重 | `src/athena/research/supervisor/scheduling.py` |
| Plan 状态/输入 | `src/athena/research/supervisor/plans.py` |
| Plan 执行与结算 | `src/athena/research/supervisor/experiment.py` |
| 崩溃恢复 | `src/athena/research/supervisor/recovery.py` |
| Idea Generation 门禁流程 | `src/athena/research/idea_generation/gate.py` |
| 候选调度与去重 | `src/athena/research/supervisor/scheduling.py` |
| 门禁判定 | `src/athena/research/idea_generation/gatekeeper.py` |
| 实时 Ideator 门禁 | `src/athena/research/idea_generation/gate.py` |
| Pairwise Elo 排序 | `src/athena/research/supervisor/scheduling.py` |
| 候选选择 | `src/athena/research/supervisor/scheduling.py` |
| Ideator 注册 | `src/athena/agents/ideator_agent.py` |
| Data Agent 注册 | `src/athena/agents/task_agents.py` |
| Agent turn 编排 | `src/athena/research/turns/runner.py`、`turns/ideator.py` |
| PREPARE/EDA 目录创建 | `src/athena/research/prepare/eda.py` |
| Baseline 研究产物与 digest 恢复 | `src/athena/research/prepare/baseline_research.py` |
| Git/OpenAlex 来源验证 | `src/athena/research/prepare/source_verification.py` |
| Baseline 两回合门禁与 Prepare Agent 注册 | `src/athena/research/prepare/baseline.py` |
| PREPARE 阶段顺序 | `src/athena/research/prepare/orchestrator.py` |
| OpenAlex 引用数元数据 | `src/athena/research/literature/paper_source/openalex.py` |
| Baseline-only Web 工具组合 | `src/athena/research/runtime/bootstrap.py`、`src/athena/research/runtime/facade.py` |
| 组合根 | `src/athena/research/runtime/facade.py` |
| GUI 图算法 | `src/athena/gui/graph.py` |
| Ideator 提示词 | `src/athena/agents/prompts/ideator_agent.md`、`src/athena/agents/prompts/ideator_gated_agent.md` |
| Data Agent 提示词 | `src/athena/agents/prompts/data_agent.md` |

### 6.1 Research 包布局与新增职责

本轮整理后的 `src/athena/research` 有 142 个 Python 文件、30,394 行；根目录只保留
10 个 Python 文件。新增文件都对应可单独测试的职责，而不是旧模块路径的兼容转发：

| 文件 | 独立职责 |
|---|---|
| `exploration_files.py` | 校验并发布 Agent exploration-file 产物 |
| `turns/__init__.py` | turns 功能包边界，不转发内部实现 |
| `turns/common.py` | turn 等待、再生成与事件公共逻辑 |
| `turns/runner.py` | 按 Agent 角色分派 turn，并持有共享运行依赖 |
| `turns/ideator.py` | Ideator lane、提示词、结果、引用与探索文件处理 |
| `turns/general.py` | General/Kaggle turn 执行 |
| `turns/support.py` | 引用支持证据验证 |
| `prepare/__init__.py` | PREPARE 功能包边界，不转发内部实现 |
| `prepare/orchestrator.py` | PREPARE 阶段顺序，不承载各步骤实现 |
| `prepare/data.py` | 数据切分与平台数据合同 |
| `prepare/baseline_research.py` | 版本化研究/验证模型、跨文件合同、digest 绑定恢复 |
| `prepare/source_verification.py` | 受限 Git 可获取性校验、OpenAlex 100 引用例外与诊断 |
| `prepare/baseline.py` | baseline 研究的单次修复/终止门禁、Prepare Agent 注册与可信执行 |
| `prepare/eda.py` | EDA 工作区、TODO 解析与执行 |
| `prepare/evaluator.py` | SEARCH/FINAL evaluator 冻结与准备 |
| `evaluation/__init__.py` | 仅公开稳定的 `TrustedEvaluator` 入口 |
| `evaluation/evaluator.py` | `TrustedEvaluator` 执行入口 |
| `evaluation/spec.py` | 数据集自适应 evaluator 文件、列与指标合同 |
| `evaluation/trust.py` | evaluator 的确定性与隔离属性检查 |
| `evaluation/validation.py` | 泛化差距与顶层验证服务 |
| `clarification/persistence.py` | clarification draft 与确认 journal 的持久化/恢复 |
| `clarification/context.py` | 已确认任务上下文的验证读取与 prompt 渲染 |
| `supervisor/scheduling.py` | outcome、Elo、候选排序与槽位调度 |
| `supervisor/plan_runtime.py` | Plan 输入、工作区、turn 执行与恢复 |
| `supervisor/settlement.py` | 指标比较、Plan 结算、SOTA 与结果投影 |
| `supervisor/validation_contracts.py` | 验证输入、结果、选项与恢复数据合同 |
| `runtime/__init__.py` | 公开稳定的 `ResearchRuntime` 与默认 survey 数量 |
| `runtime/event_projection.py` | Supervisor 事件到输出/状态的纯投影 |
| `runtime/phase_runner.py` | PREPARE、SEARCH、VALIDATE 的阶段动作 |
| `literature/__init__.py` | literature 功能包边界，不转发子系统实现 |
| `literature/contracts.py` | Source/Markdown 转换边界共享类型 |
| `paper_markdown/models.py` | 转换请求、解析态、供应商协议与持久化产物合同 |
| `paper_markdown/tex_render.py` | TeX 节点、引用和参考文献到 Markdown 的纯变换 |
| `paper_markdown/tex_tables.py` | TeX 表格识别与结构化转换 |
| `paper_markdown/pdf_elements.py` | PDF block、阅读顺序、表格与引用提取 |
| `paper_source/fetcher.py` | 下载通道、payload 识别、locator 与转换请求构造 |
| `paper_rag/search.py` | 检索、引用、章节、视觉和 chunk 图遍历 |
| `survey/pipeline.py` | Survey 请求、运行时状态与 Scout/Fetch/Convert/Index 各阶段执行 |
| `survey/providers.py` | embedding 与视觉模型生产实现 |

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

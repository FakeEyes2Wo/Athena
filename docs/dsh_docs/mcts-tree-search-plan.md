# MCTS 树搜索与流水线式 Ideation — 实施计划

Status: 待实施（active）
Owner: Athena maintainers
Last verified: 2026-08-11
Scope: SEARCH 阶段的结构性搜索（把「每轮平行开 N 候选、比完留一个」的宽度优先，升级为 MCTS/UCB1 树搜索），并叠加「下一轮 ideation 与上一轮 code-agent 实验重叠」的流水线。
Source of truth: `src/athena/research/supervisor/{scheduler,ranker,policy,supervisor}.py`、
`src/athena/research/agent_turn_runner.py`、`src/athena/core/research_tree.py`

## 1. 目标与成功标准

在**有限实验预算**（`search_limit`）下，用 MCTS/UCB1 在 hypothesis DAG 上做树搜索，替换当前「只在当前 SOTA 下展开、比完留一个」的贪心宽度优先；并让 ideation（LLM 假设生成）与 code-agent 实验（训练 + 打分）在时间上重叠，缩短 SEARCH 墙钟时间。

1. **分支选择**：MCTS 的 selection（UCB1）+ expansion（ideator 子假设）+ backprop（真实 test_score）能在预算内选择更值得展开的 lineage 分支，而非只从 SOTA 贪心前进。
2. **流水线**：下一轮 ideator 在上轮 code-agent 运行期间预热，重叠 LLM 假设生成延迟与训练/打分延迟。
3. **确定性 / 可复现 / 可回退**：MCTS 统计量是 `ResearchTree` 的纯函数（可崩溃恢复）；固定 seed 可复现；配置开关回退到现 `Scheduler`（`search_strategy="rolling"`）行为不变。

**成功标准**：`uv run pytest -q tests test/unit` 全绿（排除 slow）；MCTS 与 rolling 在同一 `ResearchTree` 快照下的选择顺序可复现；`search_strategy="rolling"` 时输出与改造前逐字节一致（回退零回归）。

## 2. 背景：当前实现的精确状态（含证据）

| # | 现状 | 证据 | 对 MCTS/流水线的含义 |
|---|---|---|---|
| A | `Scheduler.next_actions` 是纯函数、确定性、不 mutate 状态，返回 `RESUME/START_NEW/START_NEXT_HYPOTHESIS/GENERATE` | `scheduler.py:20-26`（`ScheduleKind`）、`111-174`（`next_actions`） | MCTS 应复用此契约，作为「包装」而非「替代」 |
| B | `Scheduler._queued` 只做 `Selector.deduplicate` + `Selector.rank`（宽度优先：按分数排队全量启动） | `scheduler.py:176-190` | 无「选择哪个分支展开」概念，只有「本轮选谁」 |
| C | `Selector` 是 UCB 风格点估计 `prior+strength+novelty−cost`，`_score` 无不确定性项 | `ranker.py:108-173`；`rubric_prior:40-50`、`novelty:62-71`、`deduplicate:74-93` | 可作为 MCTS 的**轻量 rollout（simulation）估值** |
| D | `HypothesisPolicy` 协议 `seed/priority/settle`；`EloPolicy.settle` 单侧固定 0.5 期望分 | `policy.py:19-57`；`queue_order:60-66` | MCTS backprop 需要一个「节点值」语义，priority 可作初值但非访问次数 |
| E | `run_ideator_turn` 用 `asyncio.gather` 并发跑 ideator lanes；`_run_ideator_lane` 单 lane；`_ideator_allocations` 分配 | `agent_turn_runner.py:100-166`、`215-260` | 流水线预热可直接复用 `_run_ideator_lane`（不重复实现） |
| F | SEARCH 循环：`_spawn_search`→`run_search`→`_fill_slots`；`asyncio.wait(FIRST_COMPLETED)` 单点等待；`_launch_turn` 启动 plan | `supervisor.py:497-512`、`523-551`、`572-618`、`620-622` | ideation 只在槽位空闲时才发生（`_fill_slots` 内），**与 code-agent 串行** |
| G | 所有新假设挂到当前 SOTA：`register_hypotheses` 硬编码 `_sota_parent()` | `supervisor.py:827-833`（`_sota_parent`）、`849-875`（`register_hypotheses`，`parent_id` 于 862 强制为 SOTA） | 树是「SOTA 链 + 每层候选扇出」的**单链**；MCTS 需允许任意父节点展开 |
| H | `start_plan` 已按 `hypothesis.parent_id` 冻结参考实验（回退 SOTA） | `supervisor.py:182-230`（参考冻结 `199-206`，`PlanInput` `214-227`） | 展开到非 SOTA 父节点时，`start_plan` **已支持**，无需改冻结逻辑 |
| I | `ResearchTree` 是现成搜索树：`root_experiment_ids/list_children/list_descendants/experiment_path/hypotheses_path` | `research_tree.py:221-255`；`add_hypothesis:119-149`；`pending_hypotheses:162-168` | MCTS 的 selection 沿 lineage 走，统计量从这些 API 纯函数导出 |

## 3. 阶段一：T1 — 引入 MCTS/UCB1 树搜索

**总原则**：MCTS 的树统计量（访问次数 N、均值 Q）**不新增独立可变状态**，而是从 `ResearchTree` 纯函数导出——`N(节点)` = 该节点为根的已 settle 后代实验数；`Q(节点)` = 这些 settle 实验在指标方向上的归一化均值（或胜率）。这样崩溃恢复 = 重载 `research_tree.json` 后重算统计量，与现有 `Recovery` 语义一致。

### 3.1 子任务 T1-a — MCTS 统计量与 UCB1（新文件 `research/supervisor/mcts.py`）

- 新增纯函数模块 `mcts.py`：
  - `node_value(experiment_id, tree) -> float`：该实验 `eval.primary` 按 `direction` 归一化（或 `ExperimentStatus.SUCCEEDED` 的均值）。
  - `visit_count(experiment_id, tree) -> int`：`list_descendants` 中已 settle（`SUCCEEDED/FAILED`）的实验数 + 自身（若 settle）。
  - `ucb1(node_id, parent_id, tree, c) -> float`：`Q + c * sqrt(ln(N(parent) + 1) / (N(node) + 1))`，`N=0` 时返回 `+inf`（强制未展开节点优先，经典 UCB1 冷启动）。
  - `select_leaf(tree, c) -> str`：从 `root_experiment_ids()` 出发，逐层选 UCB1 最大子节点（tie-break 用 `Hypothesis.order` 保证确定性），直到叶（无子或存在可展开的未 settle 节点）。
- 新增 `MctsConfig`（pydantic，frozen）：`c: float = 1.0`（UCB1 探索常数）、`simulate_rollouts: int = 1`（每节点轻量 rollout 次数）、`use_selector_score: bool = True`。

### 3.2 子任务 T1-b — `MctsScheduler`（包装现 `Scheduler`，不改 `next_actions` 契约）

- `MctsScheduler` 实现与 `Scheduler` 相同的 `next_actions(state, tree, running_ids, *, human_next, manual) -> list[ScheduleAction]`，内部：
  1. **selection**：`select_leaf(tree, c)` 选出要展开的叶节点（`human_next`/`manual` 仍优先、绕过 MCTS，保持人工语义）。
  2. **expansion**：发出 `GENERATE`，但把「目标父实验」作为新上下文传下去（见 3.3），使 ideator 子假设挂到选中的叶节点而非 SOTA。
  3. **simulation**：对 expansion 产出的候选用 `Selector.rank`（`prior+strength+novelty−cost`）做轻量 rollout 估值，**不跑真实实验**；据此排序进入 `START_NEW`。
  4. **backprop**：不在调度器内做——由 `supervisor._settle_plan` 在实验 settle 时更新 `ResearchTree`（真实 `eval.primary`），MCTS 统计量下次从树重算即可（纯函数，天然 backprop）。
- **与 `Selector`/`Scheduler` 的关系（决策点，见 §8）**：`MctsScheduler` 是**包装**——selection/expansion 用 MCTS，simulation 复用 `Selector.rank`，其余槽位填充（RESUME/START_NEW 的 `turn_limit`/`patience` 语义、`count_search_attempts` 预算、`manual`/`human_next` 分支）委托给被包装的 `Scheduler`。
- **预算/并发语义映射**：`search_limit` = 真实实验总数（不变）；`concurrency` = 每轮并行 rollout（真实实验）数（不变）；`simulate_rollouts` 只消耗 LLM 调用与 `Selector` 计算，**不消耗 `search_limit`**。

### 3.3 子任务 T1-c — 展开到任意父节点（改 `register_hypotheses`）

- `supervisor.register_hypotheses`（`849-875`）增加可选 `parent_id: str | None = None`：缺省仍 `_sota_parent()`（回退语义不变）；给定则校验 `parent_id` 属于当前项目且 `ExperimentStatus.SUCCEEDED` 后用作 `Hypothesis.parent_id`。
- `_fill_slots`（`572-618`）的 `GENERATE` 分支：当调度器是 `MctsScheduler` 且返回了「目标父实验」时，把该父实验 id 透传给 `run_ideator_turn` → `register_hypotheses(parent_id=...)`；`start_plan` 已按 `hypothesis.parent_id` 冻结参考（`supervisor.py:199-206`），无需改动。
- `propose_hypothesis`（`835-847`）同样接受可选 `parent_id` 覆盖，保持与 `register_hypotheses` 一致。

### 3.4 子任务 T1-d — 接线 + 确定性回退

- `ResearchRuntime.__init__`（`runtime.py:107-143`）与 `Supervisor.__init__`（`supervisor.py:107-144`）增加 `search_strategy: Literal["rolling", "mcts"] = "rolling"`；`"mcts"` 时构造 `MctsScheduler(policy, selector, mcts_config)` 传给 `Supervisor`。
- 确定性：UCB1 tie-break 用 `order`；无随机数；ideator 的 LLM 采样是唯一非确定源（与现状一致）。
- 回退：`"rolling"`（默认）走原 `Scheduler`，行为逐字节不变；`"mcts"` 但树无 settle 节点时，`select_leaf` 退化为「从 SOTA 展开 + `Selector.rank` 全量启动」，等价 rolling。

### 3.5 测试与验收

- **测试**（新 `test/unit/research/supervisor/test_mcts.py`；扩展 `test_scheduler.py`）：
  - `ucb1` 数学（`N=0` 返回 inf、探索常数生效、tie-break 按 order）。
  - `select_leaf` 在构造树上的选路径可复现且与预期一致。
  - `MctsScheduler.next_actions`：`human_next`/`manual` 优先级不破坏；`search_limit`/`concurrency` 预算语义与 `Scheduler` 一致。
  - `register_hypotheses(parent_id=...)`：合法父实验成功、非法父实验报 `KeyError/ValueError`、缺省仍挂 SOTA。
  - 回退：`search_strategy="rolling"` 输出与原 `Scheduler` 完全一致（快照对比）。
- **验收**：`uv run pytest test/unit/research/supervisor/test_mcts.py test/unit/research/supervisor/test_scheduler.py test/unit/research/supervisor/test_supervisor.py -q`。

## 4. 阶段二：T2 — 流水线 / 投机式 ideation

**目标**：在上一轮 code-agent 实验仍在跑时，预热下一轮 ideator，重叠「LLM 假设生成」与「训练/打分」延迟；不污染冻结候选全集。

### 4.1 子任务 T2-a — 预热缓冲（`Supervisor` 内，不落盘）

- `Supervisor.__init__` 增加 `self._prewarm: asyncio.Task | None = None` 与 `self._prewarm_buffer: list[Hypothesis] = []`。
- `_fill_slots`（`572-618`）启动 `START_NEW` 后、`_launch_turn` 返回前：若 `self._prewarm is None` 且剩余 `create_budget > 0`，则 `self._prewarm = asyncio.create_task(self._run_ideator_turn(prewarm_count))`（`prewarm_count = concurrency`，即下一轮槽位量）。
- **预热不注册**：预热结果先落 `_prewarm_buffer`，**不调用 `register_hypotheses`**、不写 `ResearchTree`（保持候选全集冻结）。

### 4.2 子任务 T2-b — 投影父节点与失效规则

- 预热 ideator 需要一个「投影父节点」：取当前「领先候选」= 已 settle 候选里 `primary` 在指标方向上最优者（若无则当前 SOTA）。
- **失效规则（决策点，见 §8）**：当本轮最终 settle 出的新 SOTA **不等于**投影父节点时，丢弃 `_prewarm_buffer`（不注册）；相等时在下一轮 `_fill_slots` 把缓冲一次性 `register_hypotheses(parent_id=新SOTA)`。
- 取消/停止（`stop()` `812-825`、`_stopped`）：取消 `_prewarm` task、清空缓冲，绝不残留。

### 4.3 子任务 T2-c — 与 MCTS 协同

- 流水线是**正交优化**：rolling 与 mcts 都可叠加。`"mcts"` 下预热 ideator 的目标父节点 = MCTS `select_leaf` 的投影（而非固定 SOTA）；`"rolling"` 下 = 当前 SOTA。
- 若 MCTS 未实现「领先候选投影」，T2 先按「当前 SOTA」预热（最保守、零语义风险），投影父节点优化作为 T2 的可选增强标注。

### 4.4 测试与验收

- **测试**（`test_supervisor.py` / 新 `test_pipeline.py`）：
  - 预热结果不落 `ResearchTree`（`pending_hypotheses` 数量不变）直到下一轮注册。
  - 投影父节点 ≠ 新 SOTA 时缓冲被丢弃，图无污染。
  - `stop()`/`_stopped` 取消预热且清空缓冲。
  - `"rolling"` + 无预热开关时行为不变（回退零回归）。
- **验收**：`uv run pytest test/unit/research/supervisor/test_supervisor.py test/unit/research/supervisor/test_pipeline.py -q`。

## 5. 依赖与顺序

```
T1-a（mcts 纯函数）─┐
T1-c（任意父展开）  ─┼─→ T1-b（MctsScheduler 包装）→ T1-d（接线+回退）
T1 需要 T1-a/b/c 齐备后才接线
                    ↓
T2-a（预热缓冲）→ T2-b（投影父+失效）→ T2-c（与 MCTS 协同）
```

- T1 内部：a（纯函数）→ b（包装调度器，依赖 a）→ c（展开任意父，可与 a 并行）→ d（接线，依赖 a/b/c）。
- T2 依赖 T1-d 稳定（尤其 `register_hypotheses(parent_id=...)` 已就位），但 T2-a/b 的「按当前 SOTA 预热」可独立于 MCTS 先行验证。
- 合并顺序建议：T1-a → T1-c → T1-b → T1-d → T2-a → T2-b → T2-c。每子任务独立提交。

## 6. 验证命令（全量）

```bash
# T1（MCTS 调度器 + 纯函数 + 回退零回归）
uv run pytest test/unit/research/supervisor/test_mcts.py \
  test/unit/research/supervisor/test_scheduler.py \
  test/unit/research/supervisor/test_supervisor.py -q

# T2（流水线 / 预热 / 失效）
uv run pytest test/unit/research/supervisor/test_supervisor.py \
  test/unit/research/supervisor/test_pipeline.py -q

# 全量（默认排除 slow）
uv run pytest -q tests test/unit

# 真实 LLM 端到端（预热 ideator 需 provider，标记 slow）
uv run pytest -q -m slow test/integration/research/
```

## 7. 验收标准（逐任务）

- [ ] T1-a：`ucb1`/`select_leaf` 纯函数可复现；`N=0` 冷启动、tie-break 正确。
- [ ] T1-b：`MctsScheduler.next_actions` 与 `Scheduler` 同契约；预算/并发语义不破坏；selection/expansion/simulation 分工正确（backprop 走树重算）。
- [ ] T1-c：`register_hypotheses(parent_id=...)` 缺省回退 SOTA；非法父实验报错；`start_plan` 参考冻结仍正确。
- [ ] T1-d：`search_strategy="rolling"` 输出与改造前逐字节一致；`"mcts"` 无 settle 节点时退化为 rolling。
- [ ] T2-a：预热不写 `ResearchTree`，候选全集冻结不变。
- [ ] T2-b：投影父 ≠ 新 SOTA 时丢弃缓冲；相等时下一轮注册。
- [ ] T2-c：rolling/mcts 均可叠加流水线；无预热开关时回退零回归。

## 8. 决策点与风险

- **MCTS 与 `Selector`/`Scheduler` 的关系**：**包装，不替代**。`MctsScheduler` 复用 `Scheduler` 的槽位填充、预算计数、`manual`/`human_next` 语义，只在「选哪个分支展开」上引入 MCTS；`Selector.rank` 降级为 simulation 的轻量 rollout 估值，不重复发明排序。
- **UCB1 的 `c` 与 budget 语义**：`c` 是 `MctsConfig.c`（默认 `1.0`），探索-利用权衡；只有真实实验（`START_NEW` 启动的 plan）消耗 `search_limit`，simulation rollout 不消耗。`concurrency` 仍为「每轮并行真实实验数」。
- **simulation 不真跑实验**：`simulate_rollouts` 用 `Selector.score` 做零成本估值，绝不为 simulation 启动 code-agent / 训练 / 打分；真实奖励只来自 backprop 的 `eval.primary`。
- **确定性 seed / 回退**：UCB1 与 tie-break（`order`）确定性，无随机数；唯一非确定源是 LLM ideator 采样（与现状一致）。`search_strategy` 默认 `"rolling"`，保证零回归；`"mcts"` 在无历史数据时退化为 rolling。
- **流水线失效不污染冻结候选全集**：预热结果先入内存缓冲、不落盘不注册；投影父 ≠ 新 SOTA 或 `stop()` 时丢弃缓冲。已冻结的候选全集（已 `register_hypotheses` 的节点）在预热期间不被增删。
- **「领先候选投影」的确定性**：当前代码无显式 RankingRound，SOTA 在每实验 settle 时逐次 compare-and-set（`supervisor.py:765-781`）。T2-b 的「投影父」以「已 settle 候选中最优」为准；若多个候选并列，用 `order` tie-break 保证确定性。
- **风险：MCTS 展开到非 SOTA 分支的预算效率**：允许展开非 SOTA 分支可能「浪费」预算在次优 lineage 上。缓解：`c` 可调 + simulation 估值门控（低于阈值的分支不展开）+ `search_limit` 总预算仍硬约束。
- **风险：流水线预热与动态 EDA 交互**：`run_ideator_turn` 可能触发补充 EDA（`agent_turn_runner.py:159-162`）。预热若触发 EDA 会在 code-agent 运行期间改 EDA 目录，须保证预热 EDA 与进行中实验的冻结数据无交叉（预热只读 EDA 目录、写独立产物路径），否则回退为「预热不触发 EDA」。

## 9. 规范引用

- 设计基线：`docs/supervisor_design.md` §2.4（SEARCH 并行 ideator、Selector 两路信号、单一 SOTA、预算语义）。
- 前序计划：`docs/dsh_docs/search-selection-upgrade-plan.md`（Selector/tie tolerance 升级；MCTS 的 simulation 复用其 `Selector.rank`，需在 T1 前确保 §8 的回退纪律一致）。
- 代码规范：`docs/代码规范.md`（导入 / 注释 / 异常 / 格式 / 测试约定；black 88 列）。
- 规范 owner 声明：`docs/README.md`（调度/选择逻辑属 `research.supervisor`；`ResearchTree` 属 `athena.core.research_tree`；MCTS 纯函数新增模块须声明 owner，不复制 `ResearchTree` 的父子/谱系语义）。

# Idea Generation 剩余任务梳理

**写作时间：** 2026-08-04（第二版：并入 ④生成侧多 Agent化 / ⑤ThreadManager 事件流 两份
提示词核对结果）→ 2026-08-05 更新：任务一/二/三/四已全部处理完（三项落地代码，一项落地
决策 + 记录新发现的缺口），见下方每节开头的【状态】标记。全量测试跑到 560 passed。
→ 2026-08-05 二次更新（全分支复审后）：复审用真实变异测试（改坏 → 跑测试 → 记录 RED →
还原）发现三处 load-bearing 逻辑改坏后全量测试仍全绿，已全部补上测试；`emit` 此前传不进
`run_full_pipeline` 也已修。测试数 560 → 565。任务一/四的【状态】按代码实际行为下调，见各节。
**范围：** 严格限定在 `workflows/search/`（Idea Generation 任务本体）+ 它需要挂接/依赖的
外部接口。不含下游消费方或与 Idea Generation 无直接关系的架构问题——具体排除项见文末。

**已完成，作为下文的前提：** P0 `run_pre_gate`；P1+P2 `run_full_pipeline`（空白挖掘 [2] →
多候选生成+去重 [3] → pre_gate [4] → 数值性审计 [5] → 三视角审阅委员会 [6] → 验证方案 [7] →
hard_gate [8] → Elo 排序 [9]）；REVISE 修订闭环；整条编排已从顺序 await 换成 langgraph
图/状态模型（`graph.py`/`state.py`），刚合并进 `feature/idea-generation-pre-gate`
（squash 后为 commit `c71e065`，已推送 origin）。

`run_full_pipeline` 现在的返回值和签名（下文任务一、二唯一能依赖的稳定接口）：

```python
async def run_full_pipeline(
    problem: ResearchProblemInput, *, gap_miner_agent, novelty_agent, domain_review_agent,
    artifacts, corpus_ref, sample_size=MAX_VERBALIZED_SAMPLES, model=None,
    thread_id=None, checkpointer=None,
) -> tuple[list[PipelineCandidateResult], list[RankedCandidate]]
```

`results: list[PipelineCandidateResult]` 带完整候选内容（package/reports/decision）；
`ranking: list[RankedCandidate]` 只带 `idea_id`/`rating`/`comparisons`/`rubric_version`/
`evidence`，两者要按 `idea_id` 交叉引用才能拿到"排名最高的那条完整假设"。

**核对方法说明：** 下面每一项的"现状"都是直接读代码/grep 全仓库/查分支名核出来的，不是照抄
任何一方（包括之前给 AI 的提示词、之前 AI 的自述）的说法。凡是提示词或口头描述与实测代码
不一致的地方，以下文标注的实测结果为准。

---

## 任务一：Hypothesis Selector

**【状态：函数已实现，尚无调用方——阻塞于 Scheduler / RecordTree】**
`hypothesis_selector.py::select_next_hypotheses`，`test_hypothesis_selector.py` 8 条测试，
含一次红→绿变异验证（排序方向）。commit `459a1d6`。下面保留原始分析——budget 是唯一接入的
资源预算形式，"已执行实验"这项没接（见任务二）。

**为什么不标"已完成"：** 全仓库没有任何生产调用点（复审 grep 确认，只有它自己的定义和单测）。
按设计它的调用方是控制平面 `Scheduler`，而 `agents/control/scheduler.py` 目前是 7 行 docstring
占位；它要读的"已执行实验"信息又绕不开 ResearchTree，而 `research/record_tree.py` 根本不存在
（`ExpCkptTree` 还没被泛化成 `RecordTree`）。两个前置都在 Idea Generation 范围之外，**这一项
在本任务内无法闭环**，不是漏做。

**文件：** `src/athena/workflows/search/hypothesis_selector.py`（当前 7 行，纯 docstring 占位）

**现状：** 全仓库 `grep -rn "hypothesis_selector\|HypothesisSelector"` 除它自己以外零匹配——
没有调用方、没有测试、没有被 `run_full_pipeline` 或任何地方引用。`run_full_pipeline` 排完序
就结束了，排序结果目前没人接着消费。

**stub 里已经写下的设计意图（原文）：**

> 选择器读取假设的证据、预期效果、资源预算和已执行实验，将候选加入优先队列并交给纯算法
> Elo 排序模块决定优先级，不在排序步骤引入 LLM 交互。涉及可行性、新颖性或优先级判断时，
> 结果必须关联版本化 rubric 与逐项证据；该优先队列设计参考主设计中提及的 Co-Scientist 思路。

**可以直接复用的既有接口：**
- `research/ranking.py::HypoPriList`/`RankedCandidate`——纯算法 Elo，不接 LLM（Global 硬约束，
  新代码不能违反）。
- `PipelineCandidateResult`（`idea_schemas.py`）——每个候选的完整审计记录（decision/reviews/
  validation_plan/revisions 全在里面），selector 要用到"资源预算""已执行实验"这类信息大概率
  从这里取。

**还没决定、需要先设计的问题（不要跳过 brainstorming 直接写代码）：**
1. Selector 是纯函数（读 `results`+`ranking`，返回排序后的候选/下一个要验证的假设）还是有状态
   的组件（要跨多轮 `run_full_pipeline` 调用维护"已经选过哪些"）？
2. "资源预算"这个约束具体是什么形状？谁提供预算数值？
3. "已执行实验"这个信息目前哪里都没有——Idea Generation 这一层完全不知道任何假设有没有真的
   跑过实验（那是 Validate 阶段的事）。selector 要读这个信息，就绕不开跟下面任务二的
   `ResearchTree` 挂接一起设计，不能分开做。

**建议的下一步：** 用 `superpowers:brainstorming` skill 走一遍，先定选择器的输入/输出契约和
状态归属，再写实施计划。

---

## 任务二：ResearchTree 挂接（`add_hypothesis`）

**【状态：已决策，未改代码】** 选了"任务一的 selector 就是候选池，落树是 Validate 阶段的事"
这条路，不改 `ResearchTree`/`Experiment` 数据模型。决策过程中发现更深一层缺口
（`HypothesisPackage` 缺 `intervention`/`expected_effect`，见下方新增小节），留给以后接手的人。

**现状（比"还没连线"更深一层，这是本文档第一版最重要的发现）：**

`workflow.py` 里有一句注释："ResearchTree 交接(add_hypothesis)不在本轮范围内"——听起来像是
"接口都在，只是这轮没空接"。**实际不是这样**：读了 `core/research/research_tree.py`
（258 行，真实现）之后发现：

- `ResearchTree` 只有 `create_node(exp: Experiment, parent_id=None)`、`get_node_by_id`、
  `get_prompt` 三个公开方法——**没有 `add_hypothesis` 方法，design.md 里提到的这个接口名字在
  代码里不存在。**
- `create_node` 强制要求一个完整的 `Experiment` 对象：
  ```python
  class Experiment(BaseModel):
      commit: CommitHash
      hypothesis: Hypothesis
      plan: ExperimentPlan
      metric_type: str
      result: str | float          # <-- 必填，且必须已经有结果
      gitwork: GitWorkBranch       # <-- 必填，指向一次真实的 git 工作分支
  ```
  也就是说，**要把一个假设放进研究树，前提是这个假设已经跑完了一次实验、有了 commit、
  有了 result**。Idea Generation 产出的是"排好序、还没验证"的候选——`Hypothesis.status`
  字段本身支持 `"PROPOSED"`（未验证）这个状态，但 `ResearchTreeNode`/`Experiment` 这一层
  完全没有"先占个位置、还没有实验结果"的路径。

**这意味着任务二不是"写一行 `tree.add_hypothesis(...)` 调用"就能完成的**——原来给了两条路
（改 `ResearchTree`/`Experiment` 数据模型 vs. Idea Generation 产出先进候选池、落树是 Validate
阶段的事），本轮**已经决定选第二条**：`hypothesis_selector.py::select_next_hypotheses` 就是
这个"候选池"——Idea Generation 的产出到这里为止，选中的候选交给谁去跑实验、跑完实验后怎么
建 `ResearchTreeNode`，是 Validate 阶段的责任，不在这轮改 `ResearchTree` 的数据模型（风险
和范围都对不上"没有时间"这个前提）。

**做这个决定时又发现一层更早的缺口**：即便以后 Validate 阶段要把 selector 选中的候选转成
`Experiment.hypothesis`（`Hypothesis` 类型，`intervention`/`expected_effect` 是必填字段），
**`HypothesisPackage`（selector 返回的 `PipelineCandidateResult.package`）根本没有
`intervention`/`expected_effect` 这两个字段**——对比 `HypothesisDraft`（LLM 直接产出，两个
字段都在）和 `HypothesisPackage`（`generate_one_strategy`/`run_full_pipeline` 落盘用的审计
对象）就能看到：构造 `HypothesisPackage` 时这两个字段被丢掉了，只有 `run_pre_gate`（P0 单
策略路径）会直接拿 `draft.intervention`/`draft.expected_effect` 建 `Hypothesis`。也就是说，
**P1+P2 这条路径产出的候选，现在没有任何地方能拼出一个合法的 `Hypothesis` 对象**——这不是
ResearchTree 那一层的缺口，是更早、`HypothesisPackage` schema 本身的缺口，且是这轮生成侧多
Agent 化之前就存在的老问题，不是这次改动引入的。

这一步没有强行写一个丢字段的 adapter 函数——那样只会做出一个看着能用、实际编译不出合法
`Hypothesis` 的假接口，比不写更糟。留给以后接手 Validate 阶段/ResearchTree 挂接的人：
1. 先给 `HypothesisPackage` 补上 `intervention`/`expected_effect`（或者等价的信息），这是
   真正的前置条件，不是可选项。
2. 再决定 `ResearchTree`/`Experiment` 的数据模型要不要改，让它能承载"尚未验证的候选假设"。
3. `core/research/research_tree.py:209` 有一条现有 TODO："还需要保存这个树的内容，从而可以
   达到断点续传。这里不允许 codex 完成"——这条持久化需求可能跟"要不要往树里写候选"互相牵连，
   动手前先弄清楚这条限制的背景（"不允许 codex 完成"具体指什么，为什么）。

**建议的下一步：** 上面 1 是一个小改动（加两个字段，改动面小、风险低，可以直接做，不需要
brainstorm）；2、3 仍然是需要先过一轮 `superpowers:brainstorming` 的架构决策。

---

## 任务三：生成侧多 Agent 化（步骤 [3]）

**【状态：已完成】** 前置分歧已问过用户，选了"按同质化证据直接推翻，不补做 Kaggle 实验"。
5 个策略 Agent（类比迁移/机制推演/反直觉假设/约束松弛/边界外推）并行，commit `14d096e`，
`test_candidate_generation.py` 13 条测试含变异验证；下游 `test_workflow.py`/`test_graph.py`
约 11 处路由 fixture 同步改掉。全量测试无回归。下面保留原始分析作为设计记录。

**对应之前给 AI 的提示词④。核对结论（写这版分析时）：完全没做。**

**现状：**
- `docs/superpowers/specs/`、`docs/superpowers/plans/` 里没有任何这个主题的文件——
  `superpowers:brainstorming` 的 HARD-GATE（先设计后实现）从未被触发，连"先问我"那个前置
  分歧问题都没被问过。
- `candidate_generation.py::generate_candidates()` 现在还是原样：**一次** Verbalized
  Sampling LLM 调用产出最多 `MAX_VERBALIZED_SAMPLES` 个带自评概率的候选，
  `deduplicate_candidates()` 还是按 `novel_hypothesis` 词集 Jaccard 相似度（默认阈值 0.8）
  纯函数去重——跟提示词"当前形态"一节描述的一字不差。
- 本地和 origin 的分支名都查过，没有任何相关分支。

**提示词④本身要求先问我的前置分歧（原样保留，因为从未被回答过）：**

设计文档 `2026-07-30-idea-generation-full-design.md` 第 9 节写了"生成阶段从『多 Agent
并行多策略』改成『单次 Verbalized Sampling 多候选』"这条决策；同一份文档第 6 节又写着
"Kaggle 对比实验（Co-Scientist 式开放生成 vs AutoSOTA 式硬约束生成）的结论直接决定步骤 [3]
该往哪种约束强度调——结果出来之前不在设计里预设答案"。那个对比实验至今没做（本次核查也确认
仓库里没有任何相关的实验记录或脚本）。

启动这个任务前必须先决定：**是以"已观察到的候选同质化"作为充分证据直接推翻单次
Verbalized Sampling 的决策，还是先补做那个悬而未决的 Kaggle 对比实验？** 这两条路产出完全
不同的下一步（前者直接进多 Agent 设计，后者这一轮先变成一个评估任务），不能替用户假设。

**如果确定要做多 Agent 化，需要问的点（提示词④原文列出的，转述保留）：**
- 多少个策略 Agent、各自策略是什么（类比迁移 / 机制推演 / 反直觉假设 / ...）？
- 每个 Agent 配不配工具——参照三视角审阅那轮的做法：只给真正需要包外事实的角色配工具。
- 去重阈值要不要重新标定：现在的 Jaccard 0.8 是为"同一次调用产出的候选"调的，多个独立
  Agent 产出的文本分布可能不一样。
- `sampling_probability` 是 Verbalized Sampling 的产物，多 Agent 形态下每个 Agent 只出一个
  候选，跨 Agent 的自评概率还可比吗？
- 成本对比：N 个 Agent 并行 vs 一次调用的 token 与延迟差。
- **可证伪的多样性指标**——没有这个指标，改完无法判断是否达成"规避 mode collapse"这个目的，
  提示词原文强调"这条别糊弄过去"。

**模块特有的硬约束（不因为换了实现方式而失效）：**
- 生成侧不自证、不自评分，判定权只在 gatekeeper（Co-Scientist 式生成/审阅分离）。
- 审阅侧不得看到生成侧的 `sampling_probability`——prompt 与 Pydantic output schema 两个泄漏面
  都要守（这条在 REVISE 闭环那轮已经踩过一次坑，schema 那面是最终审阅才发现的，不要重犯）。

**建议的下一步：** `superpowers:brainstorming`，第一句话就问前置分歧那个问题。

---

## 任务四：候选级细粒度可观测性事件（步骤 [5]-[8]）

**【状态：事件已产出并可从唯一入口开启；消费方待定，属 execution/app_server 的决策】**
`candidate_node` 在 `deps.emit` 非 None 时把候选子图从 `ainvoke` 换成 `astream`，
screen/novelty/三视角/辩论闭环逐节点发 `idea_generation/step`，`data` 里带 `candidate_index`
区分并发候选。commit `fe4a7f0`，含红→绿变异验证。

**复审补正：** `fe4a7f0` 落地时 `emit` 只有 `run_graph` 收，`run_full_pipeline` 的签名里没有
它——而 `workflow.py` 自己声明"`run_full_pipeline` 仍是唯一入口"，调用方想拿候选级事件就必须
绕过它自己组装 `PipelineDeps`，而组装 `PipelineDeps` 正是这个入口存在的意义。已补上 `emit`
参数透传 + 一条从 `run_full_pipeline` 一路验到候选子图的测试。

**谁消费这些事件仍未定，且不该由本任务定：** `Scheduler` 是 7 行占位，
`execution/thread_manager.py` 不存在（真实实现在 `app_server/thread_manager.py`），而
CLAUDE.md 明写四种 agent 执行形态"分层未定"。沿用 paper_scout 的先例——先把域事件发出去，
分层留给它的 owner。⑤(b)（AgentControl/ThreadManager 层次关系）仍未动，见文末排除项。

**对应之前给 AI 的提示词⑤的 (a) 分支。核对结论（写这版分析时）：没有专门做过，但这次图编排
重构顺带做出了一个更粗粒度的版本，够不上 ⑤ 想要的粒度——是"部分存在但需要加细"，不是
"从零开始"。**

**⑤ 提示词原本给的两条路：**
- (a) 按 PaperScout 先例给 Idea Generation 加领域事件（`idea_generation/candidate_screened`、
  `/candidate_audited` 这类粒度），范围小、能独立交付。
- (b) 真的去定义 `AgentControl` 与 `ThreadManager` 的层次关系，范围大、跨团队。

这两条从未被问过、从未被回答。(b) 不属于 Idea Generation 任务本身，见文末排除项；下面只谈
跟 Idea Generation 直接相关的 (a)。

**现状（(a) 部分实测）：**

图编排重构的 Task 7（这次会话独立做的，动机是"给节点边界加可观测性"，并非直接响应这份
提示词⑤，只是恰好用了同一个 PaperScout 命名约定）在 `graph.py::run_graph` 里加了一个可选
`emit` 回调，会发：

```
idea_generation/started
idea_generation/step      # data={"node": node_name}
idea_generation/completed
```

但粒度跟 ⑤ 想要的差得远：
- `idea_generation/step` 只在**顶层图节点**边界发（`gap_mining`/`generate`/`candidate`/
  `collect`/`rank`），不是 `/candidate_screened`、`/candidate_audited` 这种候选内部阶段级别。
- 最终整分支审查里记录过这个缺口（Important 级，当时判定"不属于这轮 Critical 修复范围，
  留作已知限制"）：`candidate_node` 内部是手动 `ainvoke` 编译好的候选子图，没有走
  `astream`，所以**一个候选从 screen 到 gate/辩论闭环的全过程，从事件流角度看只是一条
  不透明的 `/step` 记录**——审阅意见、hard_gate 的 `blocking_factor`、辩论轮次这些信息全部
  被这一条粗事件盖住了，读事件流的人看不到候选内部发生了什么。

**如果要补齐 ⑤(a) 想要的粒度，需要问的点（提示词⑤原文列出的，转述保留）：**
- 事件粒度：每候选一条还是每阶段一条？并发下（Send fan-out 出去的候选并行跑）事件顺序怎么
  保证有意义？
- 事件里放什么：CLAUDE.md 规定事件只带小摘要与 artifact 引用，那 `blocking_factor`、
  Elo 排名结果这些算不算"小摘要"？
- 技术路径：现在候选子图是手动 `ainvoke`，要拿到候选内部粒度的事件，大概率要把它也换成
  `astream(subgraphs=True)` 或类似机制——这本身是对现有实现的改动，不是纯增量。

**建议的下一步：** 这个不需要从零 brainstorm（整体方向已经定了，`emit` 回调的骨架已经在），
但候选子图要不要接入 `astream`、事件粒度具体切到哪一级，值得过一轮小范围设计确认再动手，
不建议直接改代码。

---

## 明确排除在外（不在这次梳理范围内，只是路过提一句）

- `research/proximity.py`（相似度图，6 行 docstring 占位）——design.md 里跟 IdeaGeneration
  是并列的独立小节，不是它的子部分，物理上也不在 `workflows/search/` 目录里。
- `agents/`、`execution/` 全是 stub——这些是 Idea Generation 产出物的下游消费方（谁来调度
  "验证这个假设"、谁提供沙箱去跑实验），比 Idea Generation 本身大得多的另一摊工作。
- **⑤(b) AgentControl 与 ThreadManager 的层次关系**——提示词⑤自己也把这条定性为"范围大、
  跨团队，产出很可能是给对方看的问题陈述+方案对比"，起步位置建议是 `main` 而非 Idea
  Generation 所在的分支。这是整个执行平面的架构问题，不是 Idea Generation 能单独决定的事，
  不算在这次梳理里。核实过的现状（供将来接手这条时用）：`AgentControl` 依旧零生产消费
  （`grep -rln "AgentControl"` 只命中它自己、`__init__.py` 导出、`test/unit/test_agent.py`）；
  `core/agent/agent.py:234-235` 那个 `case "response_completed": break` 不消费完
  provider 异步生成器的已知缺陷也还在，未修——这条缺陷会影响 Idea Generation 自己用到的
  `gap_miner_agent`/`novelty_agent`/`domain_review_agent`（它们都走 `core.agent.Agent`），
  但修复地点在 `core/agent/`，不在 `workflows/search/`，且提示词⑤自己也提醒"至少一处假
  provider 的计数逻辑依赖这个当前行为，修它会连带影响那些测试"——不建议顺手改，需要单独立项。

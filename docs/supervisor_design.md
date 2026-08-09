# Athena Supervisor 设计基线

> 状态：设计已逐段确认，等待最终书面审阅。本文记录截至 2026-08-09 的首版设计。
> 本文描述目标架构，不代表当前代码已经实现。

## 1. 背景与当前问题

当前 `SupervisorAgent` 只是确定性演示骨架：收到非空输入后创建一个
`DataAgent`，登记等待，唤醒后返回固定的 `outcome://supervisor`。它不能读取项目
阶段、预算、ResearchTree、阶段事实或子 Agent 结果，因此无法承担项目编排。

现有流程还存在以下结构问题：

- `ProjectRuntime` 直接串联 Init、Data、Reflection、Ideator、Code 和验证步骤，
  Supervisor 不是唯一流程所有者。
- `_phase` 与 `projected_phase()` 同时存在，形成两套可能冲突的阶段真相。
- SEARCH 把每次候选硬编码为改进，尚未接入 ranker、evaluator、comparator 和
  search policy 的完整边界。
- Agent tree、mailbox、agent wait 和 human wait 主要保存在进程内，重启恢复能力
  与现有设计声明不一致。
- 当前工作树已经删除 ReportAgent 注册和 REPORT 阶段，但旧设计与测试仍要求六
  阶段流程。相关测试基线为 37 passed、8 failed，失败均来自这项规格冲突。

## 2. 已确认目标

### 2.1 Supervisor 形态

首版 Supervisor 是确定性协调子系统，不使用 LLM 决策，也不注册为
`AgentRuntime` 中的 root Agent。删除以下概念：

- `SupervisorAgent` 业务 Agent；
- `supervisor` registry 类型；
- `root_supervisor_id`；
- root Supervisor mailbox、私有记忆和 agent wait。

未来若引入 LLM，只替换 Planner。PlanValidator、PlanExecutor、PlanJournal 和项目
事实合同保持不变。

### 2.2 唯一公开门面

删除 `ProjectRuntime` 类和 `src/athena/research/project_runtime.py`。现有
`ResearchRuntime` 改造成 Supervisor 子系统的公开门面和项目 composition root。
`src/athena/app_server/` 保持不动。

目标依赖关系：

```text
App Server
  -> GUI Gateway / protocol adapter
  -> ResearchRuntime
       -> SupervisorCoordinator
       -> DeterministicSupervisorPlanner
       -> PlanValidator
       -> PlanExecutor
       -> PlanJournal
       -> ProjectStateStore
       -> AgentRuntime
            -> Init/Data/Plot/Reflection/Ideator/Code workers
```

`ResearchRuntime` 负责公开调用面、项目依赖装配、execution 生命周期、恢复、状态
查询和事件发布。具体规划、校验、执行与持久化由独立组件承担。

### 2.3 工作流范围

独立 REPORT 阶段和 ReportAgent 被删除。目标工作流为：

```text
TASK_CONFIGURE -> PREPARE -> SEARCH -> VALIDATE -> COMPLETED
```

完成时不生成 FinalReport 或 `ResearchResultBundle`。调用方通过状态和事实查询读取
EDA report、EvalSpec、baseline、SOTA、实验日志、ablation 和 final_test refs。

### 2.4 数据隔离

任何业务 Agent 运行前，确定性 DatasetService 必须冻结不可变的 raw、train、test 和
final_test refs。InitAgent 只能读取 training schema 和允许的 training sample；
DataAgent 只能读取 training partition；CodeAgent 只能直接读取 train，受信任评估服务
读取 test。test 是 SEARCH 中所有候选共同使用的排序集；final_test 从 PREPARE 起封存，
只能在 VALIDATE 对冻结 SOTA 执行恰好一次。任何 Agent 均不能直接获得 final_test
labels ref。

`TASK_CONFIGURE` 把输入数据摄取为项目受管的不可变 DatasetManifest，复制内容、计算
哈希并生成 dataset refs；后续流程不再依赖用户原始绝对路径。raw manifest 永不原地
修改。数据工程新增列通过新的 DerivedDatasetManifest 表达，并记录父数据版本、变换
代码 ref、schema、各 split 输出 refs、行身份映射和创建来源。派生版本必须保留原切分
边界，不能借新增列重新混合 train、test 或 final_test。

原始列是永久只读不变量。任何派生版本都必须完整保留全部原始列及其名称、类型、
行身份和逐值内容；禁止覆盖、重命名、删除或改变原始列。标准化、缺失值填补、编码、
裁剪和其他数据工程结果只能写入不冲突的新列。DatasetService 在接受派生版本前逐列
校验原始 schema 与内容哈希，并拒绝新列名称与任一原始列冲突。

DataAgent 和 CodeAgent 都可以提交候选特征变换代码，但均无权直接写入受管数据。
DatasetService 是唯一变换执行者和 DerivedDatasetManifest 提交者：变换在 train 上
拟合，使用冻结参数分别应用到 test；进入 VALIDATE 后才允许把同一冻结变换应用到
final_test。服务接受版本前验证原始列不变、切分边界不变、新列无名称冲突，
并保存变换代码 ref、参数 ref 和创建 Agent/Run。

派生数据版本形成 append-only DAG。每个版本绑定一个明确
`parent_dataset_ref`；同父版本的不同候选允许并行分支，不自动合并。实验必须记录
其精确 dataset version。只有获胜实验引用的分支可以随 SOTA 被接受。若需要组合两个
分支，必须创建新的显式候选变换与数据版本，并作为独立实验重新评估。

首版默认切分比例为 train 70%、test 15%、final_test 15%。DatasetService 按任务
选择策略：classification 按 target 分层，regression 使用确定性随机切分，time-series
按时间顺序切分且禁止 shuffle，存在 group/entity 配置时按 group 切分并禁止同组跨
split。比例、策略、seed、时间列和 group 列写入 DatasetManifest；`TASK_CONFIGURE`
可以在 execution 启动前覆盖，启动后冻结。

inspect 与 split 完成后、DataAgent EDA 之前必须冻结 EvalSpec。EvalSpec 固定 target、
主指标、方向、evaluator 入口、test 对齐规则和输出合同。EDA、baseline 和 SEARCH
均只能使用该版本；execution 运行中不得根据数据发现或实验结果修改评分口径。

EvalSpec 可以包含可选的 `target_test_score`。每个 SEARCH round 完整收尾后，确定性
SearchPolicy 按指标方向判断当前 SOTA 是否达到目标：maximize 使用大于等于，minimize
使用小于等于，并沿用 EvalSpec 的 `math.isclose` tolerance 处理边界。达到目标后提交
持久化 `TARGET_REACHED` 检查点，不再生成新 round；正在运行的同轮候选仍须完整收尾，
final_test 保持封存。未配置目标时只使用预算和连续无改进停止条件。

interactive execution 在该检查点创建一种正式 `target_decision` HumanRequest，并进入
`WAITING_FOR_HUMAN`。Human 可以选择 `ACCEPT_CURRENT_SOTA`，由 Planner 提交
`SEARCH_STOPPED(reason=TARGET_ACCEPTED)` 后进入 VALIDATE；也可以选择
`RAISE_TARGET`，提交在指标方向上严格优于当前 SOTA、且超出 tie tolerance 的新目标。
后者增加 EvalSpec/SearchPolicy version、关闭请求并从当前 SOTA 继续 SEARCH。旧的
`TARGET_REACHED` 检查点永久保留用于审计，不能覆盖。

auto execution 在 `TARGET_REACHED` 自动选择 `ACCEPT_CURRENT_SOTA`，不等待 Human。
检查点、开放 HumanRequest、当前 SOTA 和 final_test sealed 状态全部存入 SQLite；因此
interactive 用户可以关闭进程，之后重新加载项目并继续作出同一个决定。

项目重载后，Human 可以读取第一次研究得到的当前 SOTA，并以该值为依据设定一个在指标
方向上更优的新 `target_test_score`。新目标形成新的 EvalSpec/SearchPolicy 版本，只影响
同一 execution 的后续搜索，不修改旧 RankingRound、历史 SOTA 或已完成 Plan。

`RAISE_TARGET` 不重置或增加任何 execution 预算：已消费的 experiments、Plans、repair
turns 和 active duration 均保持不变。接受新目标前，Validator 必须根据当前剩余预算和
成本预检确认至少还能启动一个完整 SEARCH round，并把预算快照写入决定记录；否则拒绝
`RAISE_TARGET`，保持原 HumanRequest 开放，让 Human 改为接受当前 SOTA、PAUSE 或 STOP。
`consecutive_no_improvement` 不因修改目标额外重置；若检查点由新 SOTA 触发，它已经在
该 RankingRound 的 SOTA 事务中按既有规则归零。

PREPARE 的 baseline 由 LLM worker 生成，固定顺序为：

```text
approved EDA report
  -> Ideator 阅读 task、Dataset descriptor、EDA report、EvalSpec
  -> 提交结构化 BaselinePlan
  -> CodeAgent 阅读 BaselinePlan 和相同证据，生成 baseline Bundle
  -> runner 执行
  -> Evaluator 在 test 上评分
  -> 成功结果提交为 baseline fact
```

BaselinePlan 必须引用获批 EDA report，不能读取 test labels 或 final_test。CodeAgent
不得绕过 Ideator 直接构造 baseline；只有产生有效 test 结果后 PREPARE 才完成。

baseline brainstorm 只创建一个 Ideator 实例。该实例在同一 BaselinePlan 中提出 2-3
个可行方向，随后选择一个推荐方案并给出基于 EDA/EvalSpec 的理由。PREPARE 不并行
多个 Ideator，也不运行 debate 或 ranker；CodeAgent 只接收被选中的推荐方案。

CodeAgent 只能对 BaselinePlan 做工程性调整，例如依赖 API、路径、资源和兼容性修复；
不得自行更换模型族、特征方案、dataset version 或评价口径。算法级变化必须 follow-up
原 Ideator 生成新版 BaselinePlan。CodeAgent 输出记录 `plan_ref`、实际实现摘要和全部
engineering deviations。

EDA report 使用版本化冻结 rubric。ReflectionAgent 阅读报告与证据后逐项
评分并引用 evidence refs；确定性 EvaluationPolicy 只按 rubric 阈值判定 pass/fail。
rubric 至少覆盖读取成功、schema/target、缺失与异常、分布、泄漏风险、切分说明、
采样稳定性、图表引用和结论证据。报告非空与至少一张图只属于结构预检，不能单独
构成批准条件。

EDA 产物保持简单：DataAgent 的主要输出就是一个 Markdown 文件 `eda_report.md`，图表
作为独立文件由该报告引用。LLM 生成脚本、运行日志和结构化检查结果作为旁证 Artifact
保存。ReflectionAgent 读取 `eda_report.md` 和这些 evidence refs，输出一个结构化 review；
通过后 Supervisor 只登记 `eda_report_ref`、`review_ref` 和 `accepted` 状态。Markdown 正文、
图片和脚本都不写入 SQLite。

SEARCH 每轮并行创建多个独立 Ideator。配置包含：

```text
ideator_count
hypotheses_per_ideator
selected_hypotheses_per_round
```

每个 Ideator 独立读取 task、获批 EDA、baseline、ResearchTree 和允许的检索证据，
提交指定数量且带来源的 Hypothesis。确定性 ranker 对合并候选池去重、校验并选择进入
CodeAgent 的假设。配置在 execution 启动前冻结，并受静态上限和剩余实验预算约束；
未进入执行评估的假设不消费 `max_search_experiments`。

首版默认值为 `ideator_count=3`、`hypotheses_per_ideator=2`、
`selected_hypotheses_per_round=2`。硬上限分别为 8、5 和 4；实际选中数不得超过剩余
实验预算。旧版词面 Jaccard/Bradley-Terry/UCB Ranker 已从当前源码删除，且其冷启动
语义不足，不作为默认复用方案。

新 Selector 组合两类信号：ReflectionAgent 按冻结 rubric 提交候选的可验证性、历史
差异、EDA 证据、实现可行性、预计成本和泄漏风险评分，解决新假设冷启动；重新实现的
Bradley-Terry/UCB 只读取真实实验 Comparator 胜负，积累历史强度和探索不确定性。
Agent 自评、rubric 分数和文本相似度不得直接伪造 BT 胜负记录。最终选择仍由确定性
Selector 完成，并使用稳定 tie-break。

Rubric 分数作为冷启动 prior；随着同 lineage/family 的真实比较数量增加，权重平滑
转向 BT mean。UCB uncertainty 奖励尚未充分探索的方向，并叠加 novelty bonus 与
cost penalty；不存在历史的全新 family 使用零均值、高不确定性 prior。

所有通过结构校验的 Ideator Hypothesis 在排名前先写入 ResearchTree/ResearchGraph，
包括本轮最终未选中的候选。Selector 的输入不是“本轮新生成列表”，而是图中全部
尚未执行且仍符合资格的 Hypothesis 加本轮新增节点。未选中只形成 deferred ranking
记录，不把科学状态改成 REFUTED 或 REJECTED；后续轮次仍可重新排名并选中。只有真实
实验结果才能把假设更新为 SUPPORTED/REFUTED，确定性硬门槛失败才能标记 REJECTED。
SEARCH 预算耗尽后，未执行节点继续保留用于审计和未来 execution。

每轮排名提交 append-only `RankingRound` Artifact/记录，至少包含候选全集、rubric
prior、BT mean、uncertainty、novelty、cost、最终分数、稳定排序、selected IDs、
deferred IDs、输入 graph version 和 policy version。Hypothesis 节点不覆盖保存“最新
分数”；历史 RankingRound 是解释选择的唯一权威记录。

每个 selected Hypothesis 对应一个独立 CodeAgent、实验记录和 Git worktree。一个
SEARCH round 的所有候选从同一冻结 SOTA commit、dataset version 和 EvalSpec 分叉，
允许并行但互不读取其他候选 worktree。只有显式组合假设可以包含多项 intervention；
普通候选不能暗中合并其他分支代码。

selected 候选失败时采用分层修复，不使整个 round 失败。基础设施瞬时错误先由 Executor
按技术重试策略处理；代码异常、依赖错误、输出合同不合格等业务失败则由 Planner 创建新
Plan，通过 `FOLLOWUP_AGENT` 把失败证据发回原 CodeAgent。修复必须继续使用同一个 Agent
thread、同一个 hypothesis、同一个 worktree 和同一个 experiment lineage，不重新 spawn
一个丢失上下文的替代 Agent。其他候选不等待该候选诊断即可继续运行。

修复耗尽后，该 Experiment 标记为 `FAILED`，Hypothesis 标记为 `INCONCLUSIVE`，表示没有
形成可用于支持或反驳假设的有效 test 证据；不得标为 `REFUTED`。同一 round 不从 deferred
候选动态补位，冻结的候选全集保持不变。剩余候选完成后仍正常冻结 RankingRound；失败候选
保留错误分类、失败指纹、每轮诊断、修改 ref 和最后日志 ref。`INCONCLUSIVE` 假设在当前
execution 不再入选，但可由后续 execution 基于完整失败历史显式重新激活。

每轮开始时冻结唯一父 SOTA。所有候选与同一父 SOTA 比较；整轮完成后，改善候选再
进行确定性比较，最终在一个事务中最多提交一个新 SOTA。完成顺序不得改变结果。
获得实验支持但未赢得本轮的 Hypothesis 仍标记 SUPPORTED 并保留在图中，不等同于
SOTA。首版不实现 Pareto front 或多父分支调度。

SEARCH 排行严格按照共同 test split 上的 primary `test_score` 和 EvalSpec 方向排序。
默认评估模式对 classical ML 和 deep learning 都使用 train 内 `k=5`：记录五个 fold
分数、mean、std 和失败情况，随后使用完整 train 再训练一次，并在 test 上评估一次。
test_score 决定排行榜和唯一 SOTA；K-fold 只提供稳定性证据。成本预检判断 K-fold 过高
时，在候选运行前确定性切换为完整 train 训练一次 + test 检验一次，不在运行中根据
结果临时改变评估模式。final_test 仍只在 VALIDATE 使用一次。

默认成本预检公式为：

```text
estimated_cost = baseline_full_train_duration * (k + 1)
                 * selected_candidates * 1.5
```

若估算成本超过剩余 active execution duration 的 40%，或单 fold 预计超过内存/operation
timeout，则整轮在启动前使用 single-test。评估模式、估算输入和降级原因写入 Plan 与
RankingRound；同一轮所有候选必须使用相同模式，不能按候选结果分别降级。

系统只保留一个 SOTA：

```text
sota_experiment_id = test leaderboard 第一名
                   = ResearchGraph 唯一 SOTA 节点
                   = 下一轮分叉父节点
                   = VALIDATE 冻结对象
```

SOTA 只按 EvalSpec 方向比较 test primary score。K-fold mean/std/fold scores 只作为
稳定性证据和 UI 风险提示，不阻止 test 第一名成为 SOTA。分数差落在 EvalSpec 的纯
数值 tie tolerance 内时视为平局；平局保留当前 SOTA，不设置业务 `min_effect`。
默认使用 `math.isclose(relative_tolerance=1e-9, absolute_tolerance=1e-12)`；两个参数
冻结进 EvalSpec。

```python
# TODO(search-cost-tiebreak): 成本指标合同获批后，可在 test score 平局时比较训练时间、推理时间和内存；首版平局始终保留当前 SOTA。
```

K-fold 是可配置的 SEARCH 证据策略，不是 SOTA 接受策略。Human 可以选择放弃 K-fold；
每次选择记录 policy version、作用范围、理由和来源，
不得改写历史 RankingRound。

K-fold policy 的作用范围是整个 execution，初始值为 `required | auto | disabled`。
Human 只能在 SEARCH round 边界修改；已启动 round 使用原 policy 完成，新 policy 从
下一轮起生效并增加 policy version。同一轮禁止部分候选 K-fold、部分候选 test-only。

首版不执行 paired bootstrap，但实现位置保留明确注释：

```python
# TODO(search-bootstrap): test predictions 重采样成本完成基准评估且 EvalSpec 获得 bootstrap seed/count/CI/min_effect 合同后，增加 paired bootstrap；首版使用 5-fold 或单次 test。
```

相关实现保留明确的后续注释：

```python
# TODO(search-pareto): 需要多目标 EvalSpec、frontier 上限/剪枝、预算分配和 VALIDATE 最终选择合同获批后，再扩展为 Pareto front；首版保持唯一 SOTA。
```

数据摄取、索引、切分、采样以及 DataAgent 的读取、解析和 EDA 都必须由 LLM 生成
脚本，不提供只针对 CSV 或单一模态的硬编码读取实现。LLM 根据受限的源 manifest、
文件结构提示和阶段合同生成脚本；DatasetService 只负责隔离执行、输出合同校验、
原始列/切分不变量验证和 refs 提交，不自行解析业务数据。

混合输入目录的文件角色和 target 语义由 LLM 决定，不由确定性平台代码推断，也不要求
Human 逐次确认。首个 DataAgent 使用自己生成的通用 inspect 脚本完整读取目录，并提交
`DatasetRoleProposal`，至少包含：发现的资源、各资源建议角色、选定的可训练来源、target
候选、任务类型、预测/评分可行性、关系或对齐假设、不确定项，以及每项判断对应的 inspect
evidence refs。生产代码不得根据文件名、列名、扩展名组合或已知数据集结构注入语义结论。

`DatasetRoleProposal` 必须经过独立 ReflectionAgent 审查。ReflectionAgent 读取 task intent、
proposal、inspect 脚本、结构化 inspect 结果和证据 refs，但不直接读取原始数据，也不生成
第二套 reader。它输出结构化 `DatasetRoleReview`：

```text
verdict: ACCEPT | REVISE
issues[]
evidence_refs[]
required_changes[]
```

`REVISE` 由 Planner 通过 `FOLLOWUP_AGENT` 交回原 DataAgent，在同一 thread、workspace 和
reader lineage 中修订脚本或 proposal，并消耗 `max_prepare_revisions`。修订后必须由
ReflectionAgent 重新审查；不得由 Planner 或 DatasetService 代替业务判断。预算耗尽仍未
`ACCEPT` 时，auto execution 进入 `FAILED(reason=DATA_ROLE_REVIEW_EXHAUSTED)`；不得强制
接受最后一版。interactive execution 创建持久化 `data_role_resolution` HumanRequest，展示
当前 proposal、最新 review、修订历史、冲突点和 evidence refs，只允许：

```text
ACCEPT_CURRENT_PROPOSAL
SUBMIT_ROLE_CORRECTION
```

第二种回答必须满足 HumanRequest 的结构化 response schema，明确资源角色、训练来源、target
和必要对齐字段。有效 HumanReply 提交独立 `HumanDatasetRoleResolution` 权威事实；它可以
显式覆盖 Reflection 的语义反对意见，但不得伪装成 `DatasetRoleReview(ACCEPT)`。随后仍必须
通过相同的确定性结构不变量校验；校验失败时请求保持开放并返回具体错误。Human 也可随时
使用公共 `STOP` 终止 execution。除此之外，Agent 仍可在确实缺少任务意图或授权时提出普通
HumanRequest；auto 模式始终按 DataAgent -> ReflectionAgent 循环全自动执行且不伪造回答。

Reflection `ACCEPT` 或有效 `HumanDatasetRoleResolution` 产生后，确定性 Validator 只检查
结构不变量：schema 完整、refs 存在且属于当前项目、被引用字段确实出现在对应 inspect
结果中、所选输入满足评分合同、原始字节和原始列未修改、输出不越权。它不重新解释数据
语义，也不按 Titanic 或其他已知数据集规则推翻 LLM/Human 结论。只有 RoleProposal、审查
或人工解决事实及结构校验全部通过后，DatasetManifest、reader 与 EvalSpec 才能冻结并进入
后续 EDA/baseline。

多阶段脚本共享一个经验证后冻结的 reader 实现，使 inspect、split、sample、EDA 和
feature transform 对同一数据保持一致解释。逻辑角色固定为 `inspect`、`split`、
`sample`、`analyze` 和 `transform`，但任何脚本或模块文件名都不固定。每个
DataScriptBundle 可以包含任意相对路径的源码文件，只需在 bundle metadata 中声明
唯一 entrypoint、逻辑角色、reader ref、runtime 和依赖。Runner 只依赖统一入口合同，
不按 `analysis.py` 等文件名寻找产物。

入口合同采用语言相对无关的 CLI + JSON：

```text
<runtime> <declared-entrypoint> --request <request.json> --output <result.json>
```

Runner 负责生成请求、限制路径与资源、检查退出码、读取输出并按阶段 schema 校验。
脚本必须原子写出 JSON 结果；退出码非零、结果缺失、超限或 schema 不符均视为该
operation 失败。Python helper 可以减少参数解析样板，但不是权威调用合同。

Python Bundle 使用 `uv` 管理动态依赖，并保持简单的草稿/冻结两态：

```text
DRAFT:
  LLM 在独立 workspace 通过命令行使用 uv init / uv add / uv run 迭代

FROZEN:
  Runner 执行 uv lock，固化源码、pyproject.toml 和 uv.lock
  后续只用 uv sync --frozen 与 uv run --frozen 执行
```

不增加独立 EnvironmentBuilder 服务。LLM 可以在 DRAFT 阶段直接进行命令行交互，
但 entrypoint 提交后不得在运行中修改依赖。Journal 保存 bundle ref、lock ref、Python
版本和环境 hash；依赖失败不能退化到宿主全局环境。

首版 Runtime registry 只实现 `python-uv`。CLI + JSON 合同继续保持语言相对无关，
但不在首轮同时实现 R、Julia、Node 或任意命令 runtime。

对应原型代码必须保留可检索且带明确退出条件的注释：

```python
# TODO(supervisor-security): 完整流程跑通后，用强隔离 runner 替换本地 workspace shell；生产/敏感数据发布前必须完成。
# TODO(data-runtime): 出现已批准的非 Python 数据脚本需求后，新增对应 runtime adapter；首版仅支持 python-uv。
```

TODO 必须放在实际临时边界旁，不能用来替代当前合同、错误处理或测试。

首轮原型允许 LLM 在独立 workspace 中直接使用命令行和 `uv` 生成、安装、试跑脚本，
并明确标记 `strong_isolation=false`。进入安全阶段后，该入口替换为受控数据脚本
runner：固定 entrypoint 在隔离环境执行，只把经过 schema 校验、大小限制和敏感内容
过滤的结果或诊断返回 Agent；运行目录、原始只读输入和待提交输出彼此分离，普通文件
工具不能借输出目录复制并读取原始数据。

EDA 默认首先完整读取 train。只有受信任执行器确认发生资源类失败（例如内存上限、
执行超时或受控进程被资源限制终止）后，Planner 才允许 follow-up 原 DataAgent 进入
自适应采样回退。普通脚本错误、解析错误或报告质量失败不能伪装成资源失败。采样
视图只用于 EDA，不进入训练或评估。

### 2.5 VALIDATE 与 ablation

进入 VALIDATE 时先冻结唯一 SOTA 的 experiment、Git commit、dataset version、
EvalSpec 和完整 accepted-hypothesis lineage。首版默认 `ablation_mode=FULL_LINEAGE`：
对 baseline 到最终 SOTA 路径上的每个 accepted intervention 分别执行一次 leave-one-out
消融，从最终 SOTA 中移除该 intervention，使用完整 train 重新训练，并在 test 上评分。
Ablation 默认不运行 K-fold，以限制验证成本；它不读取 final_test、不更新排行榜，也不能
改变已冻结 SOTA。

每个 accepted intervention 必须在 ResearchGraph 中保存显式 `depends_on` 边。逐项消融
不是强行删除一个会破坏下游的节点，而是计算该 intervention 的传递依赖闭包，并从最终
SOTA 中同时移除它和所有依赖它的后继 intervention。AblationRecord 分别保存
`requested_intervention_id`、`removed_dependency_closure` 和
`ablation_kind=single|dependency_group`。因此结果解释为“该 intervention 及依赖它的能力
整体贡献”，不允许 CodeAgent 临时重写下游组件来制造一个未经 SEARCH 的替代实现。

每个 AblationRecord 至少保存：

```text
intervention_id
full_sota_test_score
ablated_test_score
score_delta
train_duration
status
artifact_refs
```

同时保留 `ablation_mode=BASELINE_ONLY`：只重新比较最终 SOTA 与 baseline，不做逐项
leave-one-out。该模式属于 Human gate，不能由 Planner、ReflectionAgent、成本预检或 auto
推荐擅自选择。切换时必须保存明确的 HumanReply、理由、当时的 lineage 长度和成本估算。
auto execution 始终使用 `FULL_LINEAGE`。无论使用哪种模式，都必须生成结构化
AblationSummary；该事实提交成功后，Supervisor 才能创建读取 final_test 的 Plan。

每个 interactive execution 第一次进入 VALIDATE 时都必须先创建持久化
`ablation_scope` HumanRequest，不以成本是否超限为触发条件。请求展示 SOTA lineage、
逐项实验数量、预计训练时间、剩余 active duration，以及 `FULL_LINEAGE` 与
`BASELINE_ONLY` 两个选项；推荐值是 `FULL_LINEAGE`，但不得自动代答。项目关闭并重载后
继续等待同一请求。auto execution 不创建此请求，直接提交 `FULL_LINEAGE` 决定记录。
HumanReply 与最终 ablation mode 提交后，才能生成任何 ablation operation。

Ablation 失败分成两层。首先必须对冻结 SOTA Bundle 做 reproducibility preflight：源码、
uv.lock、dataset version、训练入口、完整 train 训练和 test prediction 合同必须可重现。
该 preflight 失败属于 SOTA validation failure，使用 `max_validation_environment_repairs`
和适用的 repair-turn 预算修复；耗尽后 execution 进入 `FAILED`，不能用 partial-ablation
gate 绕过。只有完整 SOTA 可重现，而某个“移除 intervention 后的解释性变体”仍失败，才
允许形成 `INCONCLUSIVE` AblationRecord。

单个 ablation 先执行技术重试，再由同一个 validation worker 在同一 lineage 上执行 repair
turn；连续相同失败指纹规则仍然生效。预算耗尽后记录请求删除项、实际依赖闭包、错误分类、
attempt/repair 历史、最后日志和 Artifact refs，但 `ablated_test_score` 保持为空。其他 ablation
继续完成，Supervisor 不把一个解释实验失败升级为整个 batch 的技术失败。

所有 ablation 到达终态后生成内容寻址的 AblationSummary。若存在 `INCONCLUSIVE`，其状态
为 `PARTIAL`，并至少保存 planned/succeeded/inconclusive counts、coverage ratio、失败项和
风险说明。interactive execution 随后创建 `ablation_incomplete` HumanRequest，绑定
`ablation_summary_ref` 及其内容哈希，只允许：

```text
PROCEED_WITH_PARTIAL
STOP_BEFORE_FINAL_TEST
```

`PROCEED_WITH_PARTIAL` 提交显式 `PartialAblationAcceptance` 事实；只有其引用的 summary
hash 与当前权威 summary 完全一致，Validator 才允许创建 final-test Plan。summary 发生任何
变化都会使旧接受记录失效并要求重新确认。`STOP_BEFORE_FINAL_TEST` 使 execution 进入
`FAILED(reason=ABLATION_INCOMPLETE_REJECTED)`，final_test 继续封存；它不调用公共 STOP，
因此不会把该决定混同为用户取消运行。auto execution 在 SOTA reproducibility preflight
成功时确定性选择 `PROCEED_WITH_PARTIAL`，并记录来源为 `auto_policy`。

final_test 采用“恰好一个逻辑评估、同一 attempt 可恢复”的语义。创建 final-test Plan 前，
Supervisor 在单个事务中冻结 SOTA/EvalSpec/dataset refs，并插入唯一 FinalTestAttempt。
唯一键至少包含 `(execution_id, sota_experiment_id, final_test_ref, eval_spec_version)`；如果
记录已存在，Planner 必须恢复它，不能创建另一个 attempt。状态机为：

```text
RESERVED -> RUNNING -> PREDICTIONS_WRITTEN -> SCORED -> COMMITTED
                    \-> FAILED_UNRECOVERABLE
```

Runner 的预测、日志和 score 都写为内容寻址 Artifact，并在状态迁移中保存哈希。Coordinator
重启后按以下规则恢复：`RESERVED` 可启动同一 attempt；`RUNNING` 先对照进程、lease、Journal
和输出 refs，不能在状态不明时并发再启动；存在完整预测时从 `PREDICTIONS_WRITTEN` 继续评分；
存在有效 score Artifact 时只执行 `SCORED -> COMMITTED` 的 compare-and-set，不再训练、预测
或评分。只有受信任 Evaluator 可以读取 final_test labels，Agent、Planner 和 Human 均不能
读取中间标签、预测明细或未提交分数。

同一逻辑 attempt 允许在尚无完整预测或 score 时，经过已批准的基础设施修复后重新启动物理
Runner；这属于崩溃恢复，不是第二次 final-test 试验。若无法确认旧进程已终止、输出完整性
无法判断，或修复预算耗尽，则标记 `FAILED_UNRECOVERABLE`，execution 进入 `FAILED`，不得
用新 attempt 绕过。首版禁止“失败后重新完整运行三次并选择一个结果”。未来若需要多 seed
重复评估，必须在 SEARCH 前把 seeds、次数和聚合公式冻结为一次复合 EvalSpec；它是正式
评价协议，不是 retry，也不得 early-stop 或选择 best-of-run。

成功提交 final_test score 后不得根据结果回滚或替换 SOTA。test leaderboard 的唯一 SOTA、
ResearchGraph SOTA 标记和最终模型引用保持不变；final_test 只追加一个 ValidationResult。
Supervisor 只保存 `test_score`、`final_test_score`、指标方向、`generalization_gap` 和布尔
`generalization_warning`，不设计 LOW/MEDIUM/HIGH 等级或额外阈值配置。maximize 指标的
gap 为 `test-final`，minimize 指标为 `final-test`；gap 为正且超过 EvalSpec 已有的
`math.isclose` tolerance 时 warning 为 true，否则为 false。warning 只表示 final_test 比
test 差，不触发第二名模型评估、重新训练、再次评分或 execution failure。只要 ablation
gate、唯一 final-test attempt 和结果合同均成功提交，execution 仍进入 `COMPLETED`；真实
但不理想的泛化表现不能被伪装成系统执行失败。

### 2.6 数据脚本威胁模型

安全入口包括用户数据、数据内潜在 prompt injection 内容和 LLM 生成脚本。受保护
资产包括原始数据、final-test labels、模型/API 凭据、宿主文件、SQLite 状态、
ArtifactStore 完整性和计算资源。主要攻击目标是数据外传、读取项目外文件、篡改
原始列或切分、注入命令以及资源耗尽。

现有 generic `bash`/`pwsh` 仅设置 workspace cwd，没有强文件系统或网络隔离。目标 runner
至少需要无网络、无宿主凭据、只读输入挂载、独立
可写输出、只读根文件系统、非特权用户、CPU/内存/PID/时间限制、输出大小限制和
fail-closed preflight。未满足强隔离时，不得在生产环境或敏感数据上执行 LLM 生成脚本。

强隔离实现暂不进入首轮“跑通完整流程”的范围。原型阶段沿用 local execution，所有
execution、事件和结果必须标记 `strong_isolation=false`，不得宣称具备数据隔离，且
仅用于非敏感测试数据。Docker/平台沙箱仍是处理真实数据和发布前的阻塞安全项；该
风险接受不能被解释为永久移除安全要求。

## 3. 外部控制面

立即删除 `SEARCH_START` 和 `VALIDATE_START` 等外部阶段推进命令。外部不能指定或
跳过研究阶段。

目标方法集合：

```text
PARSE_INTENT
TASK_CONFIGURE
RUN
PAUSE
RESUME
STOP
STATUS
REQUESTS_GET
HUMAN_REPLY
TREE_GET
TREE_SAVE
TREE_LOAD
```

“加载项目”不新增 App Server API：调用方以已有 `project_root` 构造或打开
`ResearchRuntime` 时，Supervisor 打开项目的 `.athena/supervisor.db`，校验 Artifact refs，
并恢复 executions、facts、ResearchGraph、RankingRounds 和当前 SOTA。`TREE_LOAD` 只用于
加载显式保存的 ResearchTree 快照，不能代替完整项目恢复，也不能覆盖数据库中的权威事实。

`RUN` 幂等地创建或复用当前活动 execution，并立即返回：

```json
{
  "execution_id": "exec_123",
  "status": "running",
  "phase": "PREPARE"
}
```

同一项目最多有一个活动 execution。不同项目可以并行。客户端断开不得取消后台
execution。

### 3.1 `Athena-cli`

新增唯一受支持的通用命令行入口，console script 名称固定为 `Athena-cli`，通过 `uv`
调用。它直接构造 `ResearchRuntime`，不经过、不修改 `src/athena/app_server/`：

```text
uv run Athena-cli run --project <project_root> --data <input_path> --task <intent> --mode interactive|auto
uv run Athena-cli status --project <project_root>
uv run Athena-cli pause|resume|stop --project <project_root>
uv run Athena-cli requests --project <project_root>
uv run Athena-cli reply --project <project_root> --request <request_id> ...
```

`run` 默认附着到 execution，持续显示稳定状态和事件；interactive 模式遇到 HumanRequest
时直接在终端收集满足 response schema 的回答，auto 模式不显示伪造的人工问题。调用方可
显式 `--detach`，此时命令在 `RUN` 返回 execution ID 后退出，后台 Coordinator 继续运行。
CLI 只转换参数、调用公开方法并渲染结果，不拥有阶段判断、重试、数据语义或项目事实。
这里固定的是平台命令名；LLM 生成的 DataScriptBundle 源文件名仍然完全自由，只固定其
已声明 entrypoint 的 CLI + JSON 合同。

首版只实现 CLI，并在对应适配器边界保留：

```python
# TODO(supervisor-ui): Athena-cli 与 ResearchRuntime 控制/事件合同稳定后，基于同一 RUN、STATUS、HumanRequest 和事件 API 增加 TUI/GUI 适配器；不得复制 Supervisor 状态机或编排逻辑，App Server 集成另行设计。
```

### 3.2 停用的历史 `workflow.py`

保留 `src/athena/workflow.py` 文件，但它不再是受支持入口，也不承担任何兼容职责。实现时
只允许把文件顶部 docstring 改成明确的停用声明，例如：

```python
"""DEPRECATED: Historical workflow only; Athena-cli, Supervisor, Agents and tests must not read, import or execute this module."""
```

完成这一次标记后，不再修改、修复、迁移、测试或维护该文件中的其他内容。新 Supervisor、
`Athena-cli`、业务 Agent、prompt、测试 fixture 和真实验收均不得读取、导入或执行它；Agent
文件工具和验收访问监控把该路径列入 denylist。`ProjectRuntime` 删除后该模块可能无法运行，
这是明确接受的结果；`python -m athena.workflow` 不提供兼容保证。除该文件自身的停用声明外，
其他代码和文档不得链接或推荐旧入口。

### 3.3 旧流程迁移范围

除 3.2 节的单文件例外外，迁移采用直接删除，不设置 deprecation window 或兼容 facade：

- 删除 `ProjectRuntime` 类、模块、导出和所有生产引用；
- 删除 `SupervisorAgent`、`supervisor` registry 类型、root supervisor mailbox/ID 和相关 prompt；
- 删除 REPORT phase、ReportAgent、FinalReport/ResearchResultBundle 的流程要求及相关 prompt；
- 删除或重写依赖这些旧合同的单元/集成测试，替换为 `ResearchRuntime`、Plan/Operation、
  HumanRequest、恢复、排行榜和 final-test 合同测试；
- 更新除停用 `workflow.py` 之外的用户/开发文档，只推荐 `Athena-cli` 和 `ResearchRuntime`；
- `src/athena/app_server/` 保持逐文件不变，不为此次迁移增加 adapter 或路由。

迁移完成的静态门槛是：除停用文件自身和本设计的背景/迁移记录外，生产代码、测试、prompt
和用户文档中不存在 `ProjectRuntime`、旧 SupervisorAgent 或 REPORT 流程引用；`Athena-cli`
不导入任何旧入口。

## 4. 事实与状态模型

### 4.1 研究阶段

研究阶段完全由已提交事实推导，不持久化可修改的 `_phase` 字段：

| 条件 | 投影阶段 |
|---|---|
| 尚无有效 `task_ref` | `IDLE` |
| 有 task，但 EDA report、EvalSpec 或 baseline 门槛未齐 | `PREPARE` |
| PREPARE 门槛已齐，但搜索尚未产生可验证的停止结果 | `SEARCH` |
| 有 successful SOTA 且搜索已按预算或策略停止 | `VALIDATE` |
| ablation 已记录且存在恰好一次有效 final-test | `COMPLETED` |

`TASK_CONFIGURE` 是提交 task 事实的命令，不形成另一套可变阶段状态。

### 4.2 控制状态

控制状态与研究阶段正交。已确认的行为如下：

- `RUNNING`：Coordinator 可以生成和执行新 Plan。
- `PAUSED`：不生成新 Plan、不启动 pending operation；已经运行的 Agent 允许完成，
  结果写入 PlanJournal，但不触发后续协调。
- `WAITING_FOR_HUMAN`：存在阻塞当前流程的开放 HumanRequest；不生成依赖该回答的
  新 Plan，收到有效回复后自动恢复协调。
- `CANCELLED`：STOP interrupt 活动 Agent、取消 pending operation；保留已提交事实、
  Artifact 和 Journal；原 execution 不可恢复。

STOP 后再次 RUN 创建新的 execution，并从已有权威事实继续。

### 4.3 业务预算

Supervisor 使用多维硬预算，而不是让所有行为共享一个 attempt 计数：

```text
max_total_plans
max_search_experiments
max_repair_turns_per_agent
max_identical_failure_repeats
max_prepare_revisions
max_validation_environment_repairs
max_consecutive_no_improvement
max_execution_duration
```

不同业务失败消费对应预算。任何硬预算耗尽后，Planner 不得继续同类返工；它只能
提交已经完成的证据，并根据阶段事实选择进入 VALIDATE、FAILED 或其他合法终态。
基础设施瞬时错误的技术重试不消费研究预算。

首版默认值：

```text
max_total_plans = 100
max_search_experiments = 20
max_repair_turns_per_agent = 8
max_identical_failure_repeats = 3
max_prepare_revisions = 2
max_validation_environment_repairs = 2
max_consecutive_no_improvement = 5
max_execution_duration = 6 hours
```

`max_repair_turns_per_agent` 不包含初始 turn。每次业务修复都是同一 Agent thread 上的一个
新 turn，必须携带上一轮的结构化失败、验证器反馈和 Artifact refs，并创建新的 Plan 留痕。
首版已确认默认允许 8 个 repair turns，比原先的 2 次 follow-up 更接近 Codex 的持续任务模式；它仍受
`max_total_plans`、阶段预算和 execution duration 的共同约束。若连续 3 个 repair turns
产生相同的规范化失败指纹，且没有新的诊断或 Artifact 变化，则提前停止该候选，避免高上限
变成无意义热循环。Human 可以在 `TASK_CONFIGURE` 时向下调整这两个值。

技术重试与 repair turn 是两套计数。首版参考 Codex 的分层默认值：普通可重试请求最多
重试 4 次，LLM 响应流断线最多重连 5 次；仅对明确白名单中的 timeout、connection reset、
HTTP 429/5xx、SQLite busy 和临时 I/O 错误生效。退避使用指数增长与 jitter，并尊重服务端
`retry-after`；鉴权失败、合同校验失败、确定性脚本异常和资源上限错误不做技术重试。
技术重试用尽后才把结构化失败交还 Planner，决定是否启动 repair turn。

`consecutive_no_improvement` 按 SEARCH round 计数，而不是按单个实验计数。一个 round
完成并冻结 `RankingRound` 时只更新一次：只要该轮至少一个候选被正式接受为新 SOTA，
计数器就重置为 0；否则加 1。同一轮有多少个未改善候选都不会重复累加。计数器更新、
`RankingRound` 提交和 SOTA compare-and-set 必须位于同一事务中，使 Coordinator 重启后
重复收尾也不会重复计数。计数达到 `max_consecutive_no_improvement` 时，Supervisor 在
当前 round 完整收尾后停止生成新的 SEARCH round，并进入后续合法阶段；它不会为提前
停止而取消已经运行的同轮候选。

若一个 round 的全部 selected candidates 在 repair-turn 预算耗尽后均为
`FAILED/INCONCLUSIVE`，仍冻结状态为 `NO_VALID_CANDIDATE` 的 RankingRound，保留当前
SOTA，并把 `consecutive_no_improvement` 增加 1。它不是 execution 失败；未达到连续
5 轮且其他预算尚有剩余时，Planner 可以从仍符合资格的 Hypothesis 创建下一轮。达到
上限后停止 SEARCH；存在 successful SOTA 时进入 VALIDATE，不存在时才进入 `FAILED`。
在认定 `NO_VALID_CANDIDATE` 前，Supervisor 必须先检查候选是否共享同一环境、依赖或
Runner 失败根因；共享基础设施故障应先完成适用的技术重试或修复流程，只有修复耗尽后
仍无有效 test score 才按无改进计数。

`TASK_CONFIGURE` 可以向下调整默认值。execution 启动后不能提高预算；未来若需要
提高，必须另行设计显式人工批准协议。

execution 配置包含交互模式：

```text
interaction_mode = interactive | auto
```

- `interactive`：所有正式 HumanRequest 都必须等待真人回答，不允许超时默认。
- `auto`：不进入 `WAITING_FOR_HUMAN`；问题候选交给确定性 AutoDecisionPolicy，
  自动决定或按策略失败，且完整记录决定来源。

首版 `auto` 允许 AutoDecisionPolicy 处理所有问题类别，包括目标列、主指标、数据
授权、预算选择和验证规则。每次自动决定必须保存 policy 名称与版本、候选值、所选
值、输入证据 refs、风险标签和时间，UI 必须明确标识该值不是人工确认。该模式的
答案采用提出问题的 worker 所给推荐值，但必须通过独立 Validator 校验。

auto 模式的 `needs_human_input` 候选必须包含：

```text
reason_code
question
options or response_schema
recommended_answer
confidence
rationale
evidence_refs
risk_label
```

worker 只能推荐，不能直接提交项目事实。AutoDecisionPolicy 验证 schema、候选范围
和证据引用后接受推荐，并以 `auto_policy` 来源审计。推荐合同缺失或无效时的回退
规则固定为：Planner 对原 worker 创建一次 `FOLLOWUP_AGENT`，反馈具体合同校验
错误并消费该 Agent 的 repair-turn 预算；修订后仍缺失或无效则 execution 进入
`FAILED`。不得使用通用空值、第一候选或临时切换 interactive。

## 5. 一步式 SupervisorPlan

Supervisor 采用方案 3：Planner 生成结构化 Plan，Validator 整体校验，Executor
执行。每份 Plan 只覆盖一个协调步骤，但一个步骤可以批量并行多个 worker。

协调循环：

```text
load snapshot
  -> recover unfinished plan, or create next one-step plan
  -> validate preconditions, permissions, budget and idempotency
  -> execute operations
  -> wait for required worker completions
  -> commit operation results
  -> reload authoritative facts
```

Plan 的最小合同：

```text
plan_id
execution_id
sequence
snapshot_version
reason_code
preconditions
operations[]
wait_policy
status
created_at
completed_at
```

Operation 的最小合同：

```text
operation_id
operation_type
idempotency_key
inputs / input_refs
preconditions
status
agent_id
run_id
result_refs
error
```

首版 Planner 是显式状态机。它根据缺失事实选择下一步，而不是把整个阶段或整个
项目提前展开成一份计划。

Operation 使用小型通用白名单：

```text
SPAWN_BATCH
FOLLOWUP_AGENT
WAIT_AGENTS
RUN_SERVICE
COMMIT_FACTS
SET_EXECUTION_STATUS
REQUEST_HUMAN
```

- `SPAWN_BATCH` 一次派发一个或多个 worker，并为每个 worker 保存稳定 dispatch
  key、agent id 和 run id。
- `FOLLOWUP_AGENT` 只延续已知原实例，必须携带修订原因和失败证据 refs。
- `WAIT_AGENTS` 把等待目标、完成策略和超时显式写入 Journal；任何终态都会唤醒
  Coordinator，不把失败伪装成未完成。
- `RUN_SERVICE` 只能调用静态注册、强类型且不编排 Agent 的确定性服务。
- `COMMIT_FACTS` 只能提交白名单 fact key，并要求对应 evidence refs 和预期
  `state_version`。
- `SET_EXECUTION_STATUS` 只改变 execution 控制状态，不能直接修改研究阶段。
- `REQUEST_HUMAN` 只接受经过 Planner 和 Validator 审核的 worker 问题候选，创建
  项目级持久化 HumanRequest。

不采用 `GENERATE_HYPOTHESES` 一类内部隐藏 spawn/wait 的领域大操作，也不采用
`RUN_PREPARE`、`RUN_SEARCH`、`RUN_VALIDATE` 阶段黑盒。

Plan 的 `operations` 是有序列表，Executor 按顺序执行并默认 fail-fast。前一
operation 失败后，后续 operation 不启动。Plan 不引入 DAG 调度器；并行只发生在
`SPAWN_BATCH` operation 内，由该 operation 统一记录每个 worker 的 dispatch 状态。
SQLite 事务不承诺回滚已经发生的 Agent 创建或外部执行副作用。

## 6. Plan 校验与执行

PlanValidator 至少检查：

- `snapshot_version` 仍是当前版本；
- 当前 control status 允许执行；
- operation 类型在静态白名单中；
- worker 类型与调用关系符合权限矩阵；
- 所有输入 refs 属于当前项目且存在；
- 阶段硬门槛和 operation 前置条件成立；
- 预算足够且消费量可计算；
- idempotency key 未成功提交；
- final-test 尚未存在，且本次 Plan 最多产生一次 final-test。

PlanExecutor 不自行决定下一步，只执行已通过校验的 Plan。worker 结果是候选
Artifact；只有确定性 evaluator、policy 和事实提交 operation 可以把候选提升为
权威项目事实。

Executor 只允许按固定技术策略重试明确分类的基础设施瞬时错误。Agent 失败、评审
不通过、代码执行失败和指标未改善均属于业务结果，不能由 Executor 盲目重放。普通
operation 遇到业务失败时，当前 Plan fail-fast 结束，下一轮 Planner 根据失败证据选择
`FOLLOWUP_AGENT`、`SPAWN_BATCH` 新实例或终止 execution。`SPAWN_BATCH` 是例外：
其中单个 worker 的业务失败作为该 batch 的终态结果收集，不取消其他 worker，也不把
整个 batch operation 标成技术失败；Planner 在后续 Plan 中分别安排修复。技术重试不
消费研究预算；业务返工必须经新 Plan、消费对应预算并完整留痕。

## 7. Journal 与崩溃恢复

每个项目使用一个 SQLite 数据库：

```text
.athena/supervisor.db
```

数据库只保存 execution、Plan、operation、少量事实索引、状态版本、预算、lease 和待发布
事件。Artifact 正文、Markdown、图片、脚本、模型和日志都由内容寻址 ArtifactStore 保存，
不写入 SQLite 大字段。首版不为 EDA、baseline 等产物分别设计复杂业务表；一个已通过步骤
只需记录 `fact_key`、`status`、`artifact_ref`、`version` 和时间。例如 EDA 只登记
`eda_report=accepted -> artifact://...`，实际 `eda_report.md` 仍是普通 Artifact 文件。

operation 状态、operation 结果 refs、事实提交、预算消费和 `state_version` 更新应
尽可能在同一个 SQLite 事务中完成。Agent 派发等外部副作用通过稳定 dispatch key
与 Journal 对账恢复，不能假设数据库事务可以覆盖 AgentRuntime。

Plan 和 operation 使用耐久化状态机：

```text
Plan: pending -> running -> completed | failed | cancelled
Operation: pending -> running -> succeeded | failed | skipped | cancelled
```

每个 operation 使用稳定 ID 和幂等键。重启恢复规则：

1. 加载活动 execution 和未完成 Plan。
2. 对照 Journal、AgentRuntime 状态与权威项目事实。
3. 已成功 operation 不重复执行。
4. running operation 若已有终态结果则补记 Journal。
5. 未启动 operation 从 pending 继续。
6. 完成当前 Plan 后重新读取事实，不恢复旧 Python 调用栈。
7. 恢复未完成 Plan 前不得生成新 Plan。

项目事实提交使用版本检查或 compare-and-set，避免重复接受 SOTA、重复消费预算或
重复运行 final-test。

### 7.1 Coordinator lease

同一项目同一时间只允许一个写 Coordinator。活动 execution 保存：

```text
owner_id
lease_generation
lease_expires_at
last_heartbeat_at
```

owner 定期 heartbeat。所有推进 Plan 或提交事实的写操作必须同时匹配 owner、未过期
租约和 lease generation。租约过期后，其他进程可通过 compare-and-set 增加
generation 并接管。旧 owner 即使恢复，也不能再提交旧 generation 的结果。非 owner
进程仍可执行 STATUS 和其他只读查询。

## 8. 后台 Coordinator

`RUN` 启动后台协调 execution 后立即返回 execution ID。Coordinator 持续执行
一步式协调循环，直到项目完成、失败、暂停或取消。

建议发布以下事件：

```text
execution/started
plan/created
operation/started
agent/completed
fact/committed
phase/changed
execution/paused
execution/completed
execution/failed
execution/cancelled
```

`STATUS(execution_id=None)` 返回当前打开项目的一个稳定、紧凑快照。未指定 ID 时优先
返回活动 execution，否则返回最近的 terminal execution；项目尚无 execution 时
`execution=null` 且 phase 为 `IDLE`。调用方可传历史 execution ID 查询其冻结终态。

```json
{
  "project_id": "project_123",
  "state_version": 42,
  "execution": {"id": "exec_123", "status": "RUNNING", "phase": "SEARCH"},
  "current": {"plan_id": "plan_9", "operation_id": "op_3", "active_agent_ids": ["agent_7"]},
  "progress": {"search_experiments": {"used": 3, "limit": 20}, "consecutive_no_improvement": {"used": 1, "limit": 5}},
  "sota": {"experiment_id": "exp_8", "test_score": 0.84, "target_test_score": 0.86},
  "budgets": {"plans_remaining": 77, "active_seconds_remaining": 15200},
  "human_request": null,
  "validation": {"ablation_status": null, "final_test_status": "SEALED", "final_test_score": null, "generalization_gap": null, "generalization_warning": false},
  "error": null,
  "refs": {"task": "artifact://task/1", "eval_spec": "artifact://eval/2", "graph": "artifact://graph/9"},
  "updated_at": "2026-08-09T12:00:00Z"
}
```

不存在的可选对象使用 `null`，不省略字段；预算使用 `used/limit` 或明确 remaining 值，不返回
主观完成百分比。HumanRequest 只内嵌 `request_id`、`kind` 和 `created_at`，完整问题由
`REQUESTS_GET` 读取。错误只返回稳定 code、简短 message 和 evidence ref，不嵌入日志。
Artifact、事件历史、RankingRound 和 Agent transcript 均只返回 refs，不展开正文。
`STATUS` 在一个 SQLite 只读事务中生成，无副作用、不创建 Artifact；`state_version` 可供
客户端轮询去重。

### 8.1 HumanRequest

首版采用 Planner 中介的 Codex 式人工交互。worker 发现缺少信息或授权时，只返回
结构化 `needs_human_input` 候选；Planner 判断该问题确有必要后生成包含
`REQUEST_HUMAN` 的 Plan，Validator 校验问题、上下文和权限，Executor 创建正式
HumanRequest。worker 不能直接阻塞 execution。HumanRequest 保存在 SQLite，至少
包含：

```text
request_id
execution_id
plan_id
reason_code
question
context_refs
input_mode
options
allow_free_text
response_schema
default_answer
expires_at
status
answer
answer_source
requesting_agent_id
created_at
answered_at
```

开放请求使 control status 投影为 `WAITING_FOR_HUMAN`。`REQUESTS_GET` 返回当前
项目开放请求；`HUMAN_REPLY` 按 `input_mode` 和 `response_schema` 校验并持久化回答，
然后自动唤醒 Coordinator。Planner 随后通过 `FOLLOWUP_AGENT` 把回答交回
`requesting_agent_id` 对应的原 worker，以保留任务上下文。人工回答是新的权威输入
事实，不能只保存在进程内 mailbox。

HumanRequest 阻塞整个 execution：Coordinator 不生成新 Plan，也不启动 pending
operation；已经运行的 Agent 允许完成并记录结果。回答后，若创建请求前 execution
为 RUNNING 则自动继续；若此前已经 PAUSED，则保持 PAUSED。

HumanRequest 支持 `single_select`、`multi_select` 和 `free_text` 输入。在
`interactive` 模式下不设置默认答案和过期时间，所有请求一直等待有效真人回答。
`auto` 模式不创建 HumanRequest；AutoDecisionPolicy 的决定以独立审计记录保存，
`answer_source` 为 `auto_policy`，不得伪装成人工回答。

## 9. 所有权边界

| 组件 | 拥有 | 不拥有 |
|---|---|---|
| ResearchRuntime | 公开门面、装配、execution 生命周期 | 具体阶段判断、worker 业务结果 |
| SupervisorPlanner | 下一步 Plan | 状态持久化、工具执行 |
| PlanValidator | 权限、门槛、预算和前置条件校验 | 编排策略 |
| PlanExecutor | 已批准 operation 的执行与记录 | 下一步决策 |
| ProjectStateStore | 权威项目事实和版本 | Agent 生命周期 |
| PlanJournal | execution/plan/operation 状态 | 研究事实内容 |
| AgentRuntime | worker、run、mailbox、worker wait | 研究阶段和项目预算 |
| ArtifactStore | 报告、图、日志和评估产物 | 阶段推进 |
| ResearchTree | 假设、实验、SOTA 关系 | Agent 调度 |

## 10. 真实端到端验收：`examples/titanic`

首版端到端验收必须执行真实链路：使用真实 LLM 调用生成数据脚本和各业务 Agent
产物，使用真实 `uv` 环境执行脚本，并实际训练、预测和评分模型。该验收不得使用 fake
provider、预录 LLM 响应、固定脚本、固定模型结果或跳过 Agent 的测试替身。确定性单元测试
仍可使用 fake，但不能代替此项验收。

验收输入目录只能是仓库现有的 `examples/titanic`。以下路径是硬禁区，验收进程及其
Agent 均不得读取、导入、复制、恢复或写入：

```text
examples/titanic-run
examples/titanic-run/**
.athena/titanic-run*
```

`src/athena/workflow.py` 按 3.2 节原地保留为停用历史文件，但验收进程及其 Agent 对该路径
同样是零读取、零导入、零执行。不得把其中任何逻辑迁移、包装或隐藏到 Supervisor、
DatasetService、prompt、验收 fixture 或生成脚本模板中。

Titanic 在这里仅是一个不透明的真实数据目录，不是平台内建数据类型。生产代码禁止按
`titanic` 名称、特定文件名、特定列名、已知 target、Kaggle 目录结构或该数据集的领域
知识分支。目录枚举、文件角色判断、schema/target 发现、可评分数据选择、切分、EDA 和
训练输入生成都必须经过前文定义的通用合同，其中所有实际数据读取和检查仍由 LLM 生成
的脚本完成。若通用流程无法正确理解该目录，验收应直接失败；修复只能改进通用合同、
prompt 或 Supervisor 行为，不能增加 Titanic 兼容层。

`examples/titanic` 中的原始文件保持逐字节只读。验收 harness 在启动前对目录内全部普通
文件计算 SHA-256 清单，结束后重新计算并要求路径集合、文件长度和哈希完全相同；任何
变化都使验收失败。DatasetService 如需摄取数据，只能把原始字节复制到独立项目的受管
raw snapshot，并再次核对源/目标哈希。原始列不允许修改；通用数据工程只能在新的
DerivedDatasetManifest 中追加不冲突列，并继续执行原始 schema、行身份和逐值哈希校验。

每次验收创建唯一的临时 `project_root`，它不得位于 `examples/titanic` 内，也不得匹配上述
禁区。`.athena/supervisor.db`、ArtifactStore、LLM 生成脚本、`pyproject.toml`、`uv.lock`、
worktree、派生数据和模型产物全部写入该临时项目。验收结束可以清理临时项目，但失败时
应保留或导出最小诊断包；无论成功或失败，都不能把运行产物写回示例目录。

验收成功至少要求：

1. `TASK_CONFIGURE -> PREPARE -> SEARCH -> VALIDATE -> COMPLETED` 由后台 Coordinator
   完整推进，且中间状态可从 `.athena/supervisor.db` 恢复；
2. 通用发现流程提交 DatasetManifest 和冻结 EvalSpec，DataAgent 的 LLM 生成脚本产出
   获批 EDA，Ideator/CodeAgent 生成并实际运行 baseline；
3. 至少完成一个真实 SEARCH round，所有有效假设进入 ResearchGraph，候选用统一
   `test_score` 排序并提交唯一 SOTA；
4. 按所选 ablation scope 完成 VALIDATE，并提交恰好一个可恢复的 FinalTestAttempt；
5. 最终状态包含 `test_score`、`final_test_score`、`generalization_gap` 和
   `generalization_warning`，同时保留 Agent transcript、脚本/锁文件、模型、评分和图引用；
6. 禁止路径零访问、示例目录前后哈希一致、受管 raw snapshot 哈希一致，且代码/日志中
   没有数据集专用 fallback 被触发。

这里验证的是 Supervisor 能否通过通用 LLM-script 数据合同处理一个真实的特殊目录形态，
而不是验证平台是否内置了 Titanic 知识。它不是周期性 CI 策略，而是实现完成门：Supervisor
实现和确定性测试完成后，负责实现的 Agent 必须手工启动一次上述真实验收，并持续处理失败，
直到完整流程通过后才可声明交付完成。

“直到通过”表示以失败证据推动通用实现、prompt、合同或配置修复；每次修复后使用新的临时
`project_root` 和 execution 从头验收。它不允许原样反复运行以碰运气、选择多次结果中的最好
分数、跳过失败阶段、降低通过条件、修改原始 Titanic 数据或加入数据集专用适配。每个独立
execution 内仍严格遵守唯一 FinalTestAttempt 合同。若遇到 API 凭据、额度、网络或模型服务
不可用等外部阻塞，应保留诊断并明确报告，恢复后继续同一交付验收任务。是否将该命令接入
CI 或定时任务留到后续发布工程决定，不属于当前 Supervisor 设计范围。

### 10.1 首版恢复测试矩阵

首版不对每个 Plan/Operation 状态做穷举故障注入，只覆盖会破坏幂等性、排行榜或
final-test 语义的四个关键检查点。这些测试使用确定性的 fake Agent/runner/LLM 和真实
SQLite/ArtifactStore，不调用真实模型；真实链路由上一节的 Titanic 交付验收覆盖。

| 故障注入点 | 重启后的必需行为 | 禁止行为 |
|---|---|---|
| Agent 已按 dispatch key 派发，但 Journal 尚未登记完整结果 | 对照 AgentRuntime 和 dispatch key 认领原 Agent/Run，等待或接回其唯一结果 | 再次 spawn、重复扣减业务预算 |
| SEARCH 的有效 test score Artifact 已写出，但 RankingRound/SOTA 事务尚未提交 | 校验原 Artifact、round snapshot 和 EvalSpec 后完成原事务，最多提交一个 SOTA | 重新训练、重新评分、因完成顺序选择赢家 |
| FinalTestAttempt 已产生有效 prediction 或 score Artifact，但 SQLite 尚未 `COMMITTED` | 从已到达的耐久状态继续评分或只完成提交；score 已存在时只提交 | 新建 attempt、重新训练、重新预测或重新评分 |
| execution 正等待开放 HumanRequest 时进程退出 | 重载同一 request ID、问题、上下文和阻塞状态；有效回复后只唤醒一次原 worker | 创建重复请求、自动代答、丢失回复或重复 follow-up |

每项测试还必须断言 lease generation fencing 生效、`state_version` 单调递增、预算没有重复
消费、Artifact refs 可解析，并且恢复前后的权威事实集合一致。首版不测试所有阶段边界和
所有 operation 状态的笛卡尔积；后续只有发现未被上述不变量覆盖的真实恢复缺陷时才增加
对应场景。

## 11. 首版范围外

当前设计没有尚待选择的产品行为。实施计划需要把已确认的事实门槛展开为确定性状态转移
和测试，但不得借此改变本设计。以下能力由文中对应 TODO 明确推迟，不阻塞首版原型：

- 强隔离数据 runner；它仍是生产环境或敏感数据发布前的阻塞项；
- 非 Python 数据 runtime；首版只实现 `python-uv`，数据格式继续由 LLM 生成脚本处理；
- paired bootstrap、Pareto front 和成本平局规则；
- TUI/GUI 适配器；首版只实现 `Athena-cli`。

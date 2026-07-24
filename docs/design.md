# Athena：自动化 AI4S 系统设计

整套框架需要异步执行。

所有 Prompt 使用英文，Pydantic 的 `description` 也统一使用英文。中文只在报告后处理阶段生成。

所有需要“评分、排序、通过、拒绝、风险、质量、新颖性、可行性”判断的部分，都必须绑定版本化 rubric。评分结果必须包含逐项证据，不能只返回总分。

本设计遵循奥卡姆剃刀原则：

- 能用普通函数或确定性 Service 完成的内容，不增加 Agent。
- 只有需要独立上下文、工具权限、多轮推理或并发执行时，才启动 Agent。
- 只保留一个工作流控制面、一个事实存储层和一个代码版本系统。
- 先保证最小闭环可运行，再启用 Elo、复杂搜索和领域扩展。
- 状态对象只保存必要字段，大对象统一保存为 artifact 引用。

对于下面设计中所有需要评分的部分，我们都需要写一个rubric，无论是AI写还是自己写。


## 1. 问题定义

输入：

- 优化目标 `G`
- 数据集 `D`
- 可选约束 `C`
- 可选初始代码仓库 `R`

输出：

- 可运行代码 `Code`
- 可复现运行配置 `RunConfig`
- 主要指标和辅助指标 `Metrics`
- 最优实验路径和必要消融结果
- 数据分析、实验分析和最终报告

第一阶段聚焦机器学习任务，保证单机 CPU/GPU 环境可运行。

第二阶段增加天文学数据处理能力，但不复制一套新框架，只增加领域配置、工具和 rubric。


### 问题分解

- 任务解析
    这一部分需要解析我们输入任务的信息，包括
    - 任务类型
        - 分类，回归，聚类
        - 异常检测，时间序列预测
        - 降维，排序或推荐
        - 图像、文本等非结构化数据任务
    - 数据类型
        - tabular
        - text
        - image
        - 时间序列
    - 预测目标
        - 目标变量或标签列
        - 需要预测的物理量或业务指标
        - 单目标任务或多目标任务
    - 优化目标
        - 分类指标：Accuracy、Precision、Recall、F1、AUC、Log Loss
        - 回归指标：MAE、MSE、RMSE、(R^2)
        - 聚类指标：Silhouette Score、Calinski-Harabasz Score
        - 资源指标：运行时间、内存占用、模型大小、推理延迟
        - 多目标指标：模型效果、计算成本和可解释性之间的权衡
    - 约束条件
        - 可用计算资源
        - 运行时间限制
        - 是否允许使用深度学习
        - 是否要求模型可解释
        后面的要求受限于实验环境暂不完成
        - 模型大小限制
        - 是否要求支持 CPU、GPU 或分布式运行
目标变量或标签列
需要预测的物理量或业务指标
单目标任务或多目标任务

- 数据分析  如何分析提供的数据集。
提供的数据集来自于真实世界真实环境。所以需要大量的数据清洗。如离群点去除，缺失值填充。
如果数据量很大，那么我们应该随机抽样进行分析。
同时我们应该更换种子，随机抽样3次，最后得到的抽样数据集。然后在抽样数据集上进行分析。


- 数据建模 根据我们数据分析的结果，进行模型选择和模型设计。
首先，如果数据量很大，我们可以选择机器学习模型和传统深度学习模型。

如果是机器学习模型，那么我们应该尝试进行特征工程，或者使用深度学习Encoder。
这里需要交给AI自行决定


    - 模型拟合
        - 深度学习模型拟合
        - 机器学习模型拟合

### 项目架构
```text

CODE
    -> qoder
    -> codex

PREPARE
    TaskParser
    -> DatasetService
    -> SamplingService
    -> DataAgent
    -> EvaluatorBuildAndFreeze
    -> Baseline

SEARCH
    ResearchAgent
    -> HypothesisSelector
    -> Supervisor
    -> CodeAgent
    -> Git Commit
    -> Sandbox Run
    -> Frozen Evaluator

VALIDATE
    Replication
    -> Ablation
    -> Final Untouched Test

REPORT
    Evidence Aggregation
    -> Final Report
```

## 2 系统架构

系统只保留四个清晰平面：

1. **控制平面：Scheduler**
   - 推进 Session 阶段。
   - 调度 Agent、工具和实验任务。
   - 管理预算、重试、并发和 Human in Loop。

2. **策略平面：Supervisor**
   - 限制 CodeAgent 和 FixProtocol 的修改边界。
   - 审查 Git diff 是否超出任务要求或破坏评估完整性。

3. **执行平面：ThreadManager + SandboxRuntime**
   - ThreadManager 管理 Agent 的 Thread、Turn 和事件。
   - SandboxRuntime 运行代码、evaluator 和实验进程。

4. **事实平面：StateStore + ArtifactStore + Git**
   - StateStore 保存小型结构化事实和事件。
   - ArtifactStore 保存数据、日志、模型、预测和报告。
   - Git 保存代码历史。

### schemas.py
```python
from pydantic import BaseModel, Field
from typing import Literal

ArtifactRef = str
CommitHash = str
ExecutionId = str

class MetricSpec(BaseModel):
    name: str = Field(description="Canonical metric name.")
    direction: Literal["maximize", "minimize"] = Field(
        description="Optimization direction."
    )

class TaskMetaData(BaseModel):
    task_type: str = Field(description="Task type.include tabular、text、image、time_series")
    data_type: str = Field(description="Primary data modality.")
    target_vars: list[str] = Field(
        default_factory=list,
        description="Target variables. Empty for unsupervised tasks."
    )
    primary_metric: MetricSpec = Field(description="Primary selection metric.")
    constraints: list[str] = Field(
        default_factory=list,
        description="Hard constraints that every experiment must preserve."
    )

class DataCard(BaseModel):
    name:str=Field("dataset name")
    description: str | None = Field(
        default=None,
        description="数据集用途和内容的简短说明。"
    )
    fingerprint: str = Field(
        description="数据集内容哈希，用于校验、缓存和版本识别。"
    )

    schema_ref: ArtifactRef = Field(
        description="数据字段、类型等 Schema 信息的引用。"
    )

    split_manifest_ref: ArtifactRef | None = Field(
        default=None,
        description="训练集、验证集、测试集切分结果的引用。"
    )



HypothesisStatus = Literal[
    "PROPOSED", "SELECTED", "SUPPORTED", "PATIENCE",
    "REFUTED", "FAILED", "REJECTED",
]


class Hypothesis(BaseModel):
    node_type: Literal["hypothesis"] = "hypothesis"
    id: str = Field(description="Unique hypothesis identifier.")
    parent_id: str = Field(description="Parent experiment identifier.")
    status: HypothesisStatus = "PROPOSED"
    payload_ref: ArtifactRef = Field(description="Immutable hypothesis payload reference.")
    statement: str = Field(description="Falsifiable hypothesis statement.")
    intervention: str = Field(description="Minimal change to test.")
    expected_effect: str = Field(description="Expected measurable effect.")
    evidence_refs: list[ArtifactRef] = Field(
        default_factory=list,
        description="Supporting and opposing evidence references."
    )
    patience_grant: int = Field(default=0, ge=0)
    patience_evidence_ref: ArtifactRef | None = None

class ExperimentPlan(BaseModel):
    hypothesis_id: str | None = Field(
        default=None,
        description="Hypothesis tested by this experiment."
    )
    kind: str = Field(
        description="Prototype, optimize, ablate, replicate, aggregate, or debug."
    )
    change: str = Field(description="Minimal intended change from the parent run.")
    run_config_ref: ArtifactRef = Field(description="Immutable run configuration reference.")
    budget: dict = Field(description="Runtime and resource limits.")
    acceptance_rule: str = Field(description="Rule used to accept the experiment result.")
```

### 线程管理

#### AthenaThread 与 ThreadManager

这里保留 Codex 风格的 `Thread -> Turn -> Event`，但只作为 Agent 执行接口，不再承担工作流调度。

- Thread：可恢复的 Agent 上下文。
- Turn：一次明确任务。
- Event：消息、工具调用、patch、审批和结果事件。
- 同一个 Thread 同时只运行一个 Turn。
- 并发通过多个 Thread 实现。

#### 模型定义

```python
class AthenaThread(BaseModel):
    thread_id: str
    session_id: str
    status: str
    context_ref: ArtifactRef

class AthenaTurn(BaseModel):
    turn_id: str
    thread_id: str
    request_ref: ArtifactRef
    status: str
    result_ref: ArtifactRef | None = None
```

#### ThreadManager

```python
class ThreadManager:
    async def start(self, session_id: str, context_ref: ArtifactRef) -> AthenaThread: ...
    async def submit(self, thread_id: str, request_ref: ArtifactRef) -> AthenaTurn: ...
    async def fork(self, thread_id: str, after_turn_id: str | None = None) -> AthenaThread: ...
    async def interrupt(self, thread_id: str, turn_id: str, reason: str) -> None: ...
    async def events(self, thread_id: str): ...
```

机制：

1. `submit` 立即返回 Turn，不等待完成。
2. Scheduler 通过事件流获取进度和结果。
3. Thread 历史由 StateStore 保存，长历史压缩为 context artifact。
4. `fork` 只允许从已完成 Turn 创建子 Thread。
5. 子 Thread 返回结构化结果 artifact，不直接修改父 Thread 历史。
6. LangGraph 负责 Session checkpoint；ThreadManager 不实现第二套 workflow checkpoint。


####




## 基础设施
### Kaggle 交互的Agent 工具
Kaggle 交互的Agent 工具
这个应该设定为可以全局设置比赛题目的一个类

这里发现kaggle官方提供了一个MCP:https://www.kaggle.com/docs/mcp
可以查看[文档](https://www.kaggle.com/docs/mcp)

这里面的tools太多，我们连接这个过后挑选里面部分tool即可
如
get_competition等
可以使用model inspector来进行这部分的挑选。


我希望的是一个最简形式：

这个类有全局option，里面的option可以选定使用的哪个比赛。
需要你进行测试。

同时拉取数据集最后的

### HuggingFace交互工具
可以从 Hugging Face 拉取固定 revision 的预训练模型进行微调。

必须记录：

- repository 与 revision
- license
- model/config/tokenizer digest
- 参数规模和预计资源
- full fine-tuning、冻结 encoder 或 PEFT 策略

规则：
- 默认 `trust_remote_code=False`。
- 只允许白名单或已审查来源。
- 先在渐进样本上 smoke run。
- 正式实验使用已缓存且固定 revision 的本地 artifact。
- Tabular 不默认套用 Transformer。

但是这里有一个问题，RAG部分或者搜索部分需要搜索到HuggingFace上在我们当前问题领域最好的model。


### utils
这里的框架还没有决定，所以在这里记录的时候只能写出功能
我们需要能够单轮对话的 不保存对话历史的function


### 统一研究树与 Git worktree

假设树和实验树只描述同一条研究因果链，因此不再维护 `RecordTree`、`HypoTree`
和 `ExpCkptTree` 三套抽象。`ResearchTree` 直接保存两种交替节点：

```text
成功 baseline(ExpCkpt)
    └── Hypothesis
          └── ExpCkpt
                └── Hypothesis
                      └── ExpCkpt
```

允许导入多个成功 baseline 根，但整棵树只有一个由主指标决定的全局 SOTA。每个
Hypothesis 必须挂在成功实验下，并且只能产生一个直接实验。普通非改进分支停止生长；
有证据的架构假设可以授予 `patience_grant`，允许该分支再执行有限个
`Hypothesis -> ExpCkpt` 步骤。

```python
class ExperimentOutcome(BaseModel):
    result_ref: ArtifactRef
    metric_name: str
    metric_value: float
    is_sota_at_completion: bool
    remaining_patience: int


class ExpCkpt(BaseModel):
    node_type: Literal["experiment"] = "experiment"
    id: str
    parent_id: str | None       # 仅 baseline 为 None
    change: str
    commit: CommitHash
    run_ref: ArtifactRef
    diff_ref: ArtifactRef | None
    outcome: ExperimentOutcome | None
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


class ResearchTree(ABC):
    async def add_baseline(self, node: ExpCkpt) -> None: ...
    async def add_hypothesis(self, node: Hypothesis) -> None: ...
    async def update_hypothesis(self, node_id: str, status: str) -> Hypothesis: ...

    async def prepare_experiment(...) -> ExperimentWorkspace: ...
    async def diff_experiment(self, experiment_id: str) -> ArtifactRef: ...
    async def checkpoint_experiment(...) -> ExpCkpt: ...
    async def update_experiment(...) -> ExpCkpt: ...

    async def path(self, node_id: str) -> list[Hypothesis | ExpCkpt]: ...
    async def hypotheses_path(self, node_id: str) -> list[Hypothesis]: ...
    async def best_experiment(self) -> ExpCkpt | None: ...
```

`outcome` 只在 `SUCCEEDED` 后存在；其余状态保持 `None`，避免在检查点顶层重复堆放
只属于评估完成阶段的字段。

`hypotheses_path(node_id)` 从所属 baseline 开始回溯，并按根到当前节点的顺序只返回
Hypothesis。这是后续把完整假设链输入 Agent 的稳定接口；调用方不需要读取树的内部
字典，也不需要自行拼接父节点。

实验代码快照只使用 Git，不设计自定义格式。具体流程如下：

1. `prepare_experiment` 从父实验 commit 创建唯一分支和独立 worktree。
2. CodeAgent、Codex 或 Qoder 只修改该 worktree。
3. `diff_experiment` 执行 `git add -A`，生成包含二进制文件的完整 diff，并写入 ArtifactStore。
4. Supervisor 审查该 `diff_ref`；`checkpoint_experiment` 只接受最后一次生成的同一引用。
5. `LocalGitWorkspace.commit(workspace, approved_diff_ref, message)` 再校验工作区与 HEAD，
   并以已审 Git tree 创建 commit；空 diff 复用当前 HEAD。
6. 实验终态后调用 `release_workspace`；放弃未提交实验则显式调用 `discard_experiment`。

`InMemoryResearchTree` 只用一把短时状态锁维护节点、索引和 SOTA，Git 子进程均在锁外
运行。`LocalGitWorkspace` 对仓库管理操作使用一把锁、对每个 worktree 使用独立锁，
所以不同实验可并行，同一实验的 diff/commit 不会竞争。实现分别位于
`src/athena/core/experiment_models.py`、`src/athena/core/research_tree.py` 与
`src/athena/core/gitutils/workspace.py`。`src/athena/core/experiments.py` 只保留稳定
导入入口和可通过 `python -m athena.core.experiments` 运行的完整使用示例。
对于 commit/remove 这类不可回滚副作用，取消会在 Git 与树状态都到达一致点后再传播。


我们在进行实验的时候应该可以接入Qcoder，让Qcoder来完成代码。为了方便调试，
应该将Human in Loop设置为一个可选项。  Human in Loop是 每次Agent完成一件事就交由用户判断，像claude code一样。
Debug设置为一个可选项。
Debug为 每次和大模型交互即交给用户判断

```bash
uv add qoder-agent-sdk
有Qcoder sdk

这里需要有一点验证实验
```
AgentMonitor  在Qcoder完成过程中我们需要监控完成过程，防止死锁 这个Monitor的过程需要设计

对于AI调用工具，我们需要一个线程管理器。项目需要能够对于sub Agent，tool call等调用新开线程，同时维护里面的优先级关系。

对于我们的工作流设计，我们采取
Supervisor的方式进行设计。

然后剩下的按照
### DataAnalysis
对于这部分我们应该尽可能对数据进行清洗。同时需要检查数据清洗的效果，同时对数据进行一个全面的EDA。

这里是需要Loop进行的。
实际上我们Agent在保存这里内容的时候也应该使用树这种方式。

这里应该保存一个csv或者其他类型的表格，同时允许Agent在目录下生成其他辅助资料或者分析代码

./data_analyze
./data_analyze/feature_process.csv
这里的feature_process.csv就是我们记录每一列分别进行了怎样处理的文件

我们通过**Agent工具** 注意，这里一定要通过工具交互。因为某些Dataset过于庞大同时表头过于丰富。这种类型的Dataset会挤占上下文。

同时我们处理之前需要对原始数据保存一个副本。

这里的数据分析需要我们列出一些关于画图美观的专家知识。
在数据分析完成后，我们通过新开一个AthenaThread的画图sub-agent。
这里需要有一个功能: 这里画图需不需要等待。如果等待，那么说明出来的图还需要丢回DataAnalysis，否则只是为了最后的美观。
生成的图片都放在
./data_analyze/EDA.md/
目录下面。注意，这里是目录

./data_analyze/EDA.md/EDA.md
1. 先生成 `EDA.md` 的文字草稿，并声明每一节需要什么图，以及图要回答什么问题。
2. `PlotAgent` 根据图规格生成图片，然后回填图片引用和观察结论。


### IdeaGeneration

#### Paper Reading

**这里再进行RAG的时候，需要将PDF转成markdown**


Paper Reading 生成可证伪的 `Hypothesis`，并通过 `ResearchTree.add_hypothesis`
挂到当前 SOTA 或仍有 patience 的实验。实验完成后，假设状态由主指标确定性更新；
需要恢复研究上下文时调用 `ResearchTree.hypotheses_path(current_node_id)`，把根到当前
节点的完整假设链交给 Agent。这里不再创建独立的 HypoTree。

同时这个代理需要进行RAG查询。
他需要查看的是


相关领域的论文，相关的blog，相关的技术报告
这里可以参考Co_Scientist论文里面的做法。

这里还需要一个基础设施:
相关领域论文检索并输入IdeaGeneration


同时我们还应该维护一个
```python
HypoPriList:List
# 这个List是一个优先队列List。我们使用ELO算法来对里面的Hypothesis进行一个排序，选出里面优先级最高的Hypo来进行实验验证。
# 这里在做的时候，在注释里面引用Co Scientist
```
#### Ranking
ELO为每个候选假设进行一个排序，最后做出最好的假设

注意，这里的排序是纯算法的，不应该引入LLM的交互。这一部分主要是为功能提效。

### Proximity
这一部分用来绘制究
假设和研究提案之间的相似度，并结合具体研究目标构建一个 proximity graph


### code generate

### evaluate

这部分用来评估

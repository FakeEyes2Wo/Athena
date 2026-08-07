# Athena 动态 Agent 编排设计

> 状态：设计已确认，待编写实施计划
> 日期：2026-08-08
> 范围：Agent 身份、调度、消息、记忆、人工等待，以及 DataAnalysis 评审闭环

## 1. 设计目标

Athena 以 Agent 作为执行层面的基本单位。`SupervisorAgent` 根据当前目标和运行结果，动态选择已注册的 Agent 类型、并发关系、重试方式与下一步；确定性运行层负责预算、权限、生命周期、持久化和人工确认。

本轮设计优先减少公共合同与心智负担：复用现有 `BaseAgent.run(AgentContext) -> AgentOutcome`，只补足 Agent 身份、按类型创建实例、消息投递和持久化等待所必需的能力。不把 Supervisor 的每一种决定建成独立 DTO，也不再引入 `AgentTask` 作为新工作流的执行单位。

## 2. 核心原则

1. Agent 是 Scheduler 的调度单位，不是 `AgentTask`。
2. `SupervisorAgent` 负责动态决策，Scheduler 只执行确定性规则。
3. Agent 之间传递消息和 Artifact 引用，不默认复制完整上下文。
4. 大对象进入 `ArtifactStore`，状态对象只保存必要字段和引用。
5. 同一 `agent_type` 可以存在多个相互独立的 Agent 实例。
6. 新请求默认创建新实例；只有显式提供 `agent_id` 才复用原实例及其私有记忆。
7. 需要评分、排序、接受或拒绝的行为必须先有 rubric；评估协议和 rubric 允许追加新条目，但不能删除已有条目。每次追加形成可追踪的新版本。
8. 跨项目全局记忆不能由 Agent 自动写入，必须由用户确认。

## 3. 架构边界

### 3.1 目录职责

具体 Agent 集中放置在一个目录中，例如：

```text
src/athena/
  agents/
    supervisor.py
    data.py
    plot.py
    reflection.py
    ...
  core/agent/
    runtime.py
    models.py
    provider.py
  core/agent_kernel/
    types.py
    store.py
    session.py
    kernel.py
    control.py
```

`src/athena/agents/` 保存业务 Agent；`src/athena/core/agent/` 只保存单 Agent 的模型采样、工具循环和 `BaseAgent` 契约；`src/athena/core/agent_kernel/` 是 Agent 身份、生命周期、mailbox、调度与恢复的唯一所有者。动态创建全新 Agent 类型暂不实现，按用户确认保留为唯一的 `TODO(dynamic-agent-type)`；首版只能实例化已注册类型。

### 3.2 职责划分

`SupervisorAgent`：

- 根据目标动态编排已注册 Agent。
- 决定启动哪些 Agent、是否并发、是否重试、等待谁以及下一阶段。
- 显式选择要注入给目标 Agent 的 Artifact 和记忆引用。
- 不能绕过预算、权限、人工确认和 rubric 的追加式演进规则。

Scheduler / Kernel：

- 维护 Agent 类型注册表和实例生命周期。
- 根据 `agent_type` 查找工厂并创建 Agent 实例。
- 使用全局并发上限和 FIFO 队列安排可运行实例。
- 持久化运行状态、等待状态和 mailbox。
- 检索可用的记忆候选，但不替 Supervisor 决定语义上下文。

具体 Agent：

- 通过统一运行接口完成一个 turn。
- 读取显式注入的上下文与引用。
- 输出 `AgentOutcome` 和持久化 Artifact。
- 不能直接修改其他 Agent 的私有记忆。

### 3.3 现有代码的唯一保留路径

当前仓库已经存在功能较完整的 `core/agent_kernel`，本设计在其上收敛，不新增第三套运行时：

- 保留 Kernel 的单命令序列器、Agent/Run 分离、FIFO、全局并发、mailbox、follow-up、父子完成通知和 generation/CAS 机制。
- `core/agent/control.py` 的旧 `AgentControl` 在调用方迁完后删除；它不能继续作为兼容执行路径。
- `app_server/ThreadRuntime` 降为 API 适配层，最终不再拥有 Agent task、future、memory 或生命周期状态。
- `ResearchRuntime` 只拥有 ResearchTree、预算和阶段事实；不再通过 `_run_task` 执行 Agent 工作，根执行改由 Kernel 中的 SupervisorAgent Run 承担。
- 当前 `experiment/supervisor.py` 是确定性搜索裁决器，应重命名为 `SearchDecisionPolicy`；它不是新的 `SupervisorAgent`。
- 当前 `DataPipeline` 对 `_data_agent.run()`、`_plot_agent.run()` 的直接调用迁为经共享 `AgentControl` 创建或 follow-up 已注册实例。

Composition Root 只创建一套共享基础设施：

```text
Application
  ├─ AgentTypeRegistry
  ├─ AgentKernel / AgentControl
  ├─ ResearchState（ResearchTree、Budget、阶段事实）
  ├─ ArtifactStore
  └─ root SupervisorAgent
```

## 4. 最小公共合同

### 4.1 Agent 身份

Agent 显式拥有三个身份字段：

```python
agent_id: str
agent_type: str
name: str
```

- `agent_id`：唯一实例标识，用于复用实例、follow-up 和恢复私有记忆。
- `agent_type`：能力类型，用于注册表查找和调度。
- `name`：暂时保留的显示字段，不参与调度。即使当前与类型信息部分冗余，也暂不移除。

### 4.2 运行接口

继续使用现有核心形态：

```python
class BaseAgent:
    async def run(self, context: AgentContext) -> AgentOutcome: ...
```

`AgentOutcome` 继续通过 `result_ref` 和 `next_context_ref` 指向持久化结果，不为每个 Agent 类型增加专用 completion DTO。

### 4.3 消息接口

mailbox 只需要一种最小消息：

```python
class AgentMessage:
    source: str
    content: str
    context_refs: list[ArtifactRef]
```

消息内容用于说明意图；正式报告、rubric、评审结果、图片和较大上下文通过 `context_refs` 传递。

两种操作共用 `AgentMessage`：

- `send(agent_id, message)`：只投递，不改变目标 Agent 的调度状态。
- `followup(agent_id, message)`：投递消息，并在目标可恢复时将它重新放入 FIFO。

不新增 `AgentRoleSpec`、`AgentInvocation`、`AgentCompletion`、`SupervisorDecision`、`ApprovalRecord`、`MemoryCandidate` 或专用 `DataAnalysisManifest` 等公共 DTO。确有需要的元数据优先作为存储层内部格式，而不是 Agent 间合同。

### 4.4 Supervisor 的最小控制面

SupervisorAgent 通过 Kernel 提供的受控工具调用以下五项命令，不直接访问 Scheduler、GraphStore 或其他 Agent 的 memory：

```text
spawn(agent_type, name, message) -> agent_id
send(agent_id, message)
followup(agent_id, message)
wait_for(agent_ids)
wait_for_human(message, context_refs)
```

`wait_for` 和 `wait_for_human` 登记持久化等待条件，并使当前 turn 在安全边界结束；它们不暂停 Python coroutine。已注册的 `agent_type` 列表在 Supervisor turn 开始时由 Kernel 注入，不再增加查询 DTO。首版不向业务 Agent 暴露关闭子树、修改注册表或创建新类型的能力。

## 5. 实例与调度语义

### 5.1 创建与复用

```text
只有 agent_type
  -> 注册表查找工厂
  -> 创建新的 agent_id
  -> 建立独立私有上下文

显式指定 agent_id
  -> 定位已有实例
  -> 投递 follow-up
  -> 恢复该实例的私有上下文
```

多个相同类型的 Agent 可以并行存在。`agent_type="data"` 不等于单例 DataAgent；调度和消息定位始终以 `agent_id` 为准。

`AgentTypeRegistry` 保存 `agent_type -> factory`。`spawn` 只接收 `agent_type`、`name` 和消息；factory 提供对应 runner、codec、工具与资源构造方式。GraphStore 只保存 `agent_type` 和配置引用，不持久化 Python runner、factory 或 `AgentSpec` 对象。恢复时 Kernel 根据 `agent_type` 重新解析 factory。

`agent_id` 使用独立不透明 ID，不再由 `name` 或树路径拼接。父子关系单独保存为 `parent_id`；路径只用于显示，不能作为身份或调度键。

### 5.2 首版队列

首版只实现：

- 一个全局并发限制。
- 一个以 `agent_id` 为元素的 FIFO 就绪队列；Run 仅作为该 Agent 一次 turn 的审计记录。
- 不实现按类型配额、权重或优先级。

当前 Kernel 的 `max_agents` 常驻容量与“逻辑实例保留到项目删除”冲突，因此不再由 Scheduler 计算逻辑 Agent 总数。项目预算可以限制创建数量；Scheduler 只限制同时执行的 Agent。IDLE 实例卸载运行资源后不占执行槽。

Supervisor 等待子 Agent 时进入 `WAITING`；等待用户确认时进入持久化的 `WAITING_FOR_HUMAN`。两种等待都释放执行槽。依赖结果或用户回复到达后，目标 Agent 重新进入 FIFO，不保留原执行槽，也不获得隐式优先级。

等待不能依赖暂停中的 Python coroutine。Agent 请求等待时，本 turn 在安全边界结束，Kernel 持久化等待条件、mailbox 游标和 `context_ref`；子 Agent 完成或用户回复到达后，Kernel 创建新的 turn 并重新排队。当前 `AgentScheduler.park()` 的进程内挂起语义不能作为持久化等待实现。

### 5.3 状态边界

概念状态至少包括：

```text
IDLE -> QUEUED -> RUNNING -> IDLE
                  -> WAITING -> QUEUED
                  -> WAITING_FOR_HUMAN -> QUEUED
                  -> turn FAILED -> IDLE 或由 Supervisor 重新 QUEUED
```

`WAITING_FOR_HUMAN` 必须可跨进程恢复。用户确认是确定性运行层处理的持久化事件，不依赖某个进程内的 Future 一直存活。

`COMPLETED` 和 `FAILED` 描述一次 turn 的结果，不表示删除 Agent 实例。完成 turn 后，逻辑 Agent 回到 `IDLE`，仍可通过 `agent_id` 接收 follow-up。

### 5.4 实例保留与恢复

Agent 逻辑实例保留到所属项目被用户显式归档或删除，首版不做 TTL 或自动垃圾回收。每个 turn 结束后立即释放模型会话、进程和执行槽，只在 StateStore 中保留必要信息：

- `agent_id`、`agent_type` 和 `name`
- 当前生命周期状态与最近 turn 结果
- 持久化 mailbox
- 指向私有记忆或压缩上下文的 `context_ref`

收到显式 follow-up 时，Kernel 根据已保存的 `agent_type` 从注册表重建运行对象，加载该 `context_ref`，再把实例放入 FIFO。项目归档后实例只读；项目被显式删除后，对其 `agent_id` 的 follow-up 才返回不可恢复错误。

生产 `SessionResourcesFactory` 使用现有 rollout/compaction 能力持久化和恢复 `ContextManager`；当前 `InMemoryResourcesFactory` 只保留给单元测试。Kernel 的 snapshot 不能通过重新创建空 `ContextManager` 冒充私有记忆恢复。

### 5.5 通用执行与唤醒流

```text
用户请求
  -> 创建或 follow-up SupervisorAgent
  -> Kernel 加载其私有记忆、项目状态和显式 ArtifactRefs
  -> Supervisor 通过受控工具选择已注册 agent_type
  -> Kernel 创建独立实例并将 agent_id 放入 FIFO
  -> Supervisor 登记等待条件，本 turn 在安全边界结束
  -> SupervisorAgent 进入 WAITING 并释放执行槽

子 Agent 完成
  -> 原子提交 Run 终态与 completion outbox
  -> completion 写入父 Agent mailbox
  -> 若 completion 命中父 Agent 已登记的等待条件，Kernel 创建新 turn
  -> 父 Agent 重新进入 FIFO，恢复私有记忆并读取结果引用
```

completion 不会无条件唤醒父 Agent。父 Agent 未等待该依赖时，消息只留在 mailbox，等待后续显式 follow-up 或其他已登记条件。这样可以防止无关子任务完成造成重复执行。

三种入口的事务语义不同：

- `send()`：只原子写入 mailbox，不创建 turn、不唤醒目标。
- `followup()`：原子写入消息并为可恢复目标创建新 turn；目标 busy 时零状态变更。
- completion：由 Kernel 权威生成并与子 Run 终态一同提交；只有命中持久化等待条件才唤醒。

### 5.6 人工确认流

```text
Supervisor 请求确认
  -> Kernel 持久化 WAITING_FOR_HUMAN、问题内容和关联 ArtifactRefs
  -> 当前 turn 结束并释放执行槽
  -> App Server 展示请求并接收用户回复
  -> 回复写入目标 mailbox
  -> Kernel 创建新 turn，将原 Agent 放回 FIFO
```

App Server 只负责协议传输和展示，不拥有审批事实。进程内 Future 可以作为在线连接优化，但不能成为唯一状态；客户端断线、服务重启或超时都不得删除等待记录。全局记忆候选的批准使用同一流程。

## 6. 记忆模型

### 6.1 三类记忆

Agent 私有记忆：

- 绑定 `agent_id`，保存该实例的 thread/turn 历史或压缩后的上下文。
- 只有显式复用 `agent_id` 才恢复。
- 新建同类型实例不会继承另一实例的私有记忆。

项目记忆：

- 保存对当前项目可复用的事实、工程故障与修复经验等。
- 可以依据确定性规则自动晋升，但必须保留来源 Artifact 或事件引用。

跨项目全局记忆：

- ReflectionAgent 只能产生候选。
- 写入前必须进入 `WAITING_FOR_HUMAN` 并取得用户确认。
- 未确认候选不能参与其他项目的默认检索。

### 6.2 上下文注入

Kernel 负责从私有记忆、项目记忆和已批准的全局记忆中检索候选；Supervisor 显式选择实际注入目标 Agent 的引用。这样可以避免“全局黑板”无限膨胀，也避免 Agent 自动继承不相关或不可信的历史。

Agent 间需要延续工作时，优先采用以下顺序：

1. 使用 Artifact 交接正式结果。
2. 使用消息说明本次意图。
3. 对原 Agent 使用 `followup(agent_id, ...)`，恢复它自己的私有记忆。
4. 只有稳定、可复用的经验才晋升为项目或全局记忆。

## 7. DataAnalysis 闭环

### 7.1 Agent 所有权

`DataAgent`：

- 执行数据检查、清洗分析和 EDA。
- 编写 `report.md`，汇总正文、图片引用和结论。
- 确保正式版本至少包含一张可在报告中解析的图片。
- 是 `DataAnalysis` 版本的唯一提交者。
- 收到评审后，由原 `DataAgent` 实例结合私有记忆生成 v2、v3 等新版本。

`PlotAgent`：

- 是通用绘图 Agent，任何需要绘图的 Agent 都可以启动它。
- 返回图片 ArtifactRef、图注和观察。
- 不拥有调用方的业务报告，也不能提交 `DataAnalysis` 版本。
- 对 DataAnalysis 而言，它是 DataAgent 使用的子 Agent，而不是报告作者。

`ReflectionAgent`：

- 只读待评 `DataAnalysis` 版本。
- 自己编写 rubric，然后严格按照 rubric 逐项评分。
- 生成评分、证据和修改意见，并把结果返回给原 DataAgent。
- 不能直接修改 `report.md`，也不能提交新的 `DataAnalysis` 版本。

`EvaluationPolicy`：

- 是普通确定性服务，不是 Agent。
- 根据评分所绑定 rubric 中的必选项、单项阈值和总分规则计算 `passed/failed`。
- 不生成新标准、不解释报告，也不决定下一步工作流。

### 7.2 产物结构

每个 `DataAnalysis` 版本是一个包含报告和图片的逻辑目录 Artifact：

```text
DataAnalysis/
  report.md
  figures/
    ...
```

旧版本必须保留。DataAgent 生成新版本时，新版本记录 `parent_ref` 指向上一版本。目录 Bundle 使用内容寻址 Manifest，属于 ArtifactStore 内部实现，不成为 Agent 公共合同：

```json
{
  "kind": "directory",
  "files": {
    "report.md": "sha256:...",
    "figures/example.png": "sha256:..."
  },
  "parent_ref": "sha256:..."
}
```

提交顺序如下：

1. 分别把 `report.md` 和图片写入 ArtifactStore，获得各自的内容引用。
2. 校验 Manifest 中只能使用规范化相对路径，禁止绝对路径、重复路径和 `..` 路径穿越。
3. 最后原子写入不可变 Manifest；只有这一步成功，新的 `DataAnalysis` 版本才存在。
4. 成功后更新 StateStore 中指向最新版本的引用；旧 Manifest 及其文件继续保留。
5. Manifest 写入前产生的单文件 Artifact 只是暂存内容，不能被当作正式 `DataAnalysis` 版本。

读取方只接收目录 Manifest 的 ArtifactRef，由 ArtifactStore 解析 `report.md` 或具体图片。业务 Agent 不依赖物理目录路径，也不需要了解 Manifest schema。

首次提交 v1 时，StateStore 为该分析链记录三个内部字段：`analysis_id`、`owner_agent_id` 和 `latest_ref`。后续提交必须由同一 `owner_agent_id` 发起，且 `parent_ref` 必须等于当前 `latest_ref`；校验成功并写入新 Manifest 后才原子更新 `latest_ref`。这些字段是存储层事实，不进入 AgentMessage 或公开 DataAnalysis DTO。

正式 `DataAnalysis` 版本还必须通过以下结构校验：

- Manifest 包含且只包含一个根级 `report.md`。
- Manifest 至少包含一个 `figures/` 下的图片 Artifact。
- `report.md` 中的每个本地图片引用都能解析到同一 Manifest 内的文件。
- 图片引用不能越过 Bundle 边界，也不能指向未提交的暂存 Artifact。

结构校验只证明报告完整可读，不评价图片是否必要、美观或支持结论；这些语义质量由 ReflectionAgent 写入 rubric 并评分。

### 7.3 完整数据流

```text
SupervisorAgent
  -> 创建 DataAgent 实例
  -> DataAgent 分析数据并编写 report.md
  -> DataAgent 按需启动一个或多个 PlotAgent
  -> PlotAgent 返回图片引用、图注和观察
  -> DataAgent 汇总并提交 DataAnalysis v1
  -> Supervisor 创建 ReflectionAgent
  -> ReflectionAgent 编写 rubric
  -> ReflectionAgent 按 rubric 评价 v1
  -> 生成 rubric/review Artifact
  -> EvaluationPolicy 确定性计算 passed/failed
  -> completion 返回 Supervisor mailbox

若 failed 且 Supervisor 决定修改：
  Supervisor followup(原 data_agent_id, 评审消息与 ArtifactRefs)
  -> 原 DataAgent 恢复私有记忆
  -> 生成 DataAnalysis v2，parent_ref = v1
  -> 再次进入只读评审

若 passed：
  Supervisor 根据预算和流程状态推进下一阶段或请求用户确认

若 failed：
  Supervisor 可以选择返工、重试、终止或请求用户确认
  但不能把该结果标记为 passed
```

`ReflectionAgent -> DataAgent` 的交接仍使用通用 `AgentMessage`。例如 `content` 表示“根据评审修订”，`context_refs` 指向待修订版本、rubric 和 review；不新增 DataAnalysis 专用消息协议。

## 8. Rubric 与追加式评估协议

已经确认：

- ReflectionAgent 必须自己写出 rubric。
- 必须先形成 rubric，再按照 rubric 评分。
- 评分结果必须包含逐项证据，不能只有总分。
- 评估协议和 rubric 不冻结，后续允许 append 新条目。
- 已有条目不能被删除；每次追加都会产生新的 rubric 版本。
- 每次评分必须记录实际使用的 rubric 版本。
- 历史评分与当时使用的 rubric 版本一起保留，不能用新版本静默覆盖。
- 不同 rubric 版本的总分不能直接比较；如需比较，应使用同一版本重新评分，或者只比较双方共有的条目。
- rubric 追加后不自动重评所有历史 DataAnalysis 版本。只有 Supervisor 需要比较指定版本时，才调度 ReflectionAgent 使用同一 rubric 版本按需重评这些版本。
- rubric Artifact 必须包含可供程序读取的稳定条目 ID、评分范围、必选标记和阈值；说明文字可以使用 Markdown，但 `EvaluationPolicy` 不解析自由文本来猜测规则。
- ReflectionAgent 只负责逐项评分与证据；`EvaluationPolicy` 确定性计算 `passed/failed`；Supervisor 根据该结论选择后续动作。
- Supervisor 可以在 `failed` 后选择返工、重试、终止或人工确认，但不能绕过硬性门槛将其改为 `passed`。

执行顺序是 ReflectionAgent 先根据任务目标、数据说明和当前评估协议生成或追加 `rubric.md`，记录本次 rubric 版本，再读取待评分报告并产生 `review.md`。如果评审中发现需要新标准，只能追加条目并产生下一版本，不能删除已有标准后重写历史评分。

## 9. 失败与恢复原则

首版不预先枚举所有故障，也不建设复杂错误分类、自动降级或自动重试矩阵。系统只保护不会写坏状态，其余问题在 Agent 实际运行结束后反馈给 Supervisor。

必须保留的最小规则：

1. 未注册的 `agent_type`、不存在的 `agent_id` 和 busy follow-up 在写 journal 前失败，不产生部分状态。
2. Agent 异常统一形成失败的 RunSummary，并把简短错误信息交给 Supervisor；Kernel 不解释业务原因，也不自动重试。
3. Artifact、DataAnalysis Manifest 和 Run 终态只在原子提交成功后生效；失败时继续使用上一正式版本。
4. `WAITING` 与 `WAITING_FOR_HUMAN` 持久化；服务重启后继续等待，不依赖原 Python coroutine 或在线连接。

Supervisor 根据真实 Run 结果、已有 Artifact 和剩余预算，动态决定 follow-up 原实例、创建新实例、重试、终止或请求用户。Plot、rubric、评分和工具的具体失败首版都走这一条通用反馈路径，不增加专用错误协议。

首版明确不做：

- 为每种 Agent 或工具设计独立错误码；
- 在执行前穷举检查所有可能的输入问题；
- Kernel 自动重跑模型或工具；
- 根据异常类型自动选择降级 Agent；
- 自动修复损坏的报告、rubric 或图片。

需要新增哪类检查，由真实端到端运行暴露的问题和可复现测试决定，再按实际风险补充。

## 10. 与当前实现的差距

当前核心可以复用：

- `BaseAgent.run(AgentContext) -> AgentOutcome`
- `AgentContext`
- `AgentOutcome(result_ref, next_context_ref)`
- `core/agent_kernel` 中的稳定 Agent/Run、命令序列器、mailbox、follow-up、FIFO 和 generation/CAS
- `ArtifactStore`
- `ToolResult.artifacts`

已识别的主要差距：

1. `core.agent.AgentControl` 与 `core.agent_kernel.AgentControl` 并存，公开导出仍指向旧控制面；必须单轨迁移后删除旧实现。
2. Kernel 当前由调用方直接传入包含 runner/codec 的 `AgentSpec`，GraphStore 甚至持有该 Python 对象；需要改为按 `agent_type` 注册、持久化类型键并在恢复时重建。
3. 当前 `role` 不能替代已确认的 `agent_type`；实例身份还由 name/path 拼接，无法自然支持同名独立实例。
4. `agent_kernel.AgentScheduler` 当前按 `RunId` 排队并长期预留 resident slot，需要改为按 `AgentId` 排队且 IDLE 可卸载。
5. Kernel 的 parked wait 和 App Server 的审批 Future 都是进程内状态，尚不能持久化 `WAITING` / `WAITING_FOR_HUMAN`。
6. 当前 `InMemoryResourcesFactory` 在恢复时创建空上下文，没有从 `context_ref` 恢复 Agent 私有记忆。
7. `AgentMessage` 当前暴露 `sequence`、接受任意 object content，缺少已确认的 `context_refs`。
8. `Agent.run()` 返回合成的 `result://...`，尚未持久化真实最终产物。
9. `DataPipeline`、`ResearchRuntime`、Ideator 和 App Server 仍各自持有执行 task 或 Agent history，生产工作流尚未统一接入 Kernel。
10. `DataTools` 仍返回本地 `artifact://path`；`ArtifactStore` 目前只保存 bytes/text，没有逻辑目录 Bundle。
11. `EvalSpec` 文档和工厂仍表达“PREPARE 后冻结”，需要改为不可删除、可追加的版本链；单个版本对象仍可保持不可变。
12. `AgentTask` 主要存在于测试和旧文档中，生产流程基本未使用，不应进入新合同。

这些差距用于约束后续实现计划，不代表本设计阶段立即修改代码。

## 11. 测试边界

首版只保留三层测试，不为尚未观察到的异常建立大规模参数化错误矩阵。

### 11.1 Kernel 核心测试

- 一个 `agent_type` 可以创建多个具有独立 `agent_id` 和私有记忆的实例。
- FIFO 与全局并发限制有效，`name` 不参与调度。
- `send` 只投递，`followup` 创建新 turn；显式 follow-up 原 `agent_id` 能恢复私有记忆。
- `WAITING` 与 `WAITING_FOR_HUMAN` 释放执行槽，并能在重启后恢复和重新入队。

### 11.2 DataAnalysis 集成测试

使用确定性的 Fake Agent 跑通以下闭环：

```text
Supervisor -> DataAgent -> PlotAgent -> DataAnalysis v1
           -> ReflectionAgent -> EvaluationPolicy
           -> follow-up 原 DataAgent -> DataAnalysis v2
```

只验证关键结果：

- v1 与 v2 都由同一个 DataAgent 实例提交，v2 的 `parent_ref` 指向 v1。
- Manifest 包含 `report.md` 和至少一张可解析图片。
- ReflectionAgent 只生成 rubric/review，不能修改报告。
- rubric append 后不自动重评历史版本；需要比较时才用同一 rubric 版本按需重评。

### 11.3 真实端到端 Smoke Test

使用小型 CSV 和真实模型运行一次完整流程，记录实际结果和失败，再决定补充哪些校验。该测试默认手动运行或通过显式标记启用，不阻塞没有模型凭据的普通测试。

测试公共行为和持久化结果，不锁定私有方法或临时内部数据结构。

## 12. 设计确认状态

总体架构、代码适配、数据流、错误处理和测试边界均已逐项确认。本设计只定义 Agent 运行层和 DataAnalysis 评审闭环；动态创建全新 Agent 类型、复杂调度配额、自动错误分类与自动降级明确留到后续，不进入首版实施计划。

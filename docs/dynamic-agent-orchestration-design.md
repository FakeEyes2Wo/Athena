# Athena 动态 Agent 编排设计

> 状态：设计基线已确认，专题细化待用户审阅
> 日期：2026-08-08
> 范围：Agent 身份、动态编排、调度边界、消息、记忆、人工等待与 DataAnalysis 评审闭环

## 0. 设计文档导航

本文是动态 Agent 编排的上位设计和索引，只定义跨模块原则、最小公共合同与不可破坏的不变量。代码级细节由 `docs/` 根目录下的专题设计负责：

| 文档 | 权威范围 |
|---|---|
| [Agent Kernel Runtime 设计](agent-kernel-runtime-design.md) | `core/agent_kernel`、注册表、实例生命周期、Scheduler、mailbox、持久化等待与恢复 |
| [SupervisorAgent 设计](supervisor-agent-design.md) | 动态编排输入、工具面、决策边界，以及 ResearchRuntime 适配 |
| [已注册 Agent 目录设计](registered-agent-catalog-design.md) | 首版静态 `agent_type`、Agent 准入规则、最小工具权限与旧角色映射 |
| [DataAnalysis Agent 工作流设计](data-analysis-agent-workflow-design.md) | DataAgent、PlotAgent、ReflectionAgent、报告 Bundle、rubric、评审和修订版本链 |
| [Agent 记忆与人工等待设计](agent-memory-human-wait-design.md) | 私有/项目/全局记忆、晋升与检索、用户确认 |

权威边界如下：

- 跨模块合同或原则冲突时，以本文为准。
- 某一模块的内部结构、状态迁移或代码适配，以对应专题设计为准。
- 专题设计不得扩大本文的公共合同；确需扩大时，必须先修改本文。
- 当前代码只用于确认迁移起点，不会反向覆盖已确认的目标边界。

## 1. 目标与范围

Athena 以 Agent 作为执行层面的基本单位。`SupervisorAgent` 根据目标和运行结果，动态选择已注册 Agent 类型、实例、并发关系和下一步；确定性运行层负责预算、权限、生命周期、持久化、评估硬门槛和人工确认。

本设计优先减少公共合同与心智负担：

- 复用 `BaseAgent.run(AgentContext) -> AgentOutcome`。
- Agent 是 Scheduler 的调度单位，不引入 `AgentTask` 作为新工作流执行单位。
- 同一 `agent_type` 可以存在多个独立实例。
- Agent 之间只使用一种消息形态，并用 ArtifactRef 交接正式结果。
- Supervisor 的动态决定通过少量受控命令表达，不为每种决定创建 DTO。

首版不做动态创建全新 Agent 类型、复杂调度配额、自动错误分类、自动降级和预防性错误矩阵。动态创建并注册新类型仅保留 `TODO(dynamic-agent-type)`。

## 2. 核心原则

1. `SupervisorAgent` 负责业务编排，Scheduler 只执行确定性规则。
2. `core/agent_kernel` 是 Agent 身份、生命周期、mailbox、调度和恢复的唯一所有者。
3. `core/agent` 只负责单 Agent 的模型采样、工具循环和运行合同。
4. 具体业务 Agent 统一位于 `src/athena/agents/`。
5. 大对象进入 `ArtifactStore`；状态存储只保留小型事实和引用。
6. 新请求默认创建新实例；只有显式 `agent_id` 才复用原实例及其私有记忆。
7. 需要评分、排序、接受或拒绝的行为必须先有 rubric。
8. 评估协议和 rubric 采用 append-only 版本链：允许追加，不能删除或改写已有条目。
9. 跨项目全局记忆必须经用户确认，Agent 只能生成候选。
10. 等待必须持久化；不能依赖暂停的 Python coroutine 或在线连接。
11. 只有需要独立上下文、工具权限、多轮推理或并发执行的职责才注册为 Agent；其余保留为确定性 Service。

## 3. 架构所有权

```text
Application / Composition Root
  ├─ AgentTypeRegistry
  ├─ AgentKernel / AgentControl
  ├─ ResearchState（ResearchTree、Budget、阶段事实）
  ├─ ArtifactStore
  └─ root SupervisorAgent

src/athena/agents/
  ├─ supervisor.py
  ├─ data.py
  ├─ plot.py
  ├─ reflection.py
  ├─ ideator.py
  ├─ code.py
  └─ report.py

src/athena/core/agent/
  └─ BaseAgent + sampling/tool loop

src/athena/core/agent_kernel/
  └─ identity + lifecycle + scheduler + mailbox + recovery
```

所有权约束：

- Registry 只保存 `agent_type -> factory`，不运行 Agent、不保存实例状态。
- Scheduler 不决定业务优先级、重试、接受/拒绝或下一阶段。
- GraphStore 不保存 Python runner、factory、model client 或 `ContextManager` 对象。
- 业务 Agent 不直接读写 Scheduler、GraphStore 或其他 Agent 的私有记忆。
- ResearchRuntime 和 App Server 只保留项目状态与外部协议适配，不拥有 Agent task、memory 或等待事实。

现有 `experiment/supervisor.py` 是确定性搜索裁决器，目标名称为 `SearchDecisionPolicy`；它不是新的 `SupervisorAgent`。

## 4. 最小公共合同

### 4.1 Agent 身份

```python
agent_id: str
agent_type: str
name: str
```

- `agent_id`：唯一实例标识，用于消息定位、follow-up 和私有记忆恢复。
- `agent_type`：注册表查找键；相同类型不代表同一实例。
- `name`：暂时保留的显示字段，可以重复，不参与调度。

### 4.2 运行接口

```python
class BaseAgent:
    async def run(self, context: AgentContext) -> AgentOutcome: ...
```

`AgentOutcome` 通过 `result_ref` 和 `next_context_ref` 指向持久化结果。类型化业务结果属于 Artifact 内容，不为每种 Agent 增加 completion DTO。

### 4.3 消息接口

```python
class AgentMessage:
    source: AgentId | None
    content: str
    context_refs: list[ArtifactRef]
```

`content` 只说明意图或简短状态；报告、rubric、review、图片和大上下文通过 `context_refs` 传递。`source=None` 保留给 Kernel 生成的内部 completion。消息顺序由 GraphStore 的提交顺序和 mailbox 游标保证，不进入公共消息合同。

调用方提交 `content + context_refs`，由 Kernel 构造权威消息 envelope：

```text
spawn(agent_type, name, content, context_refs=[]) -> agent_id
send(agent_id, content, context_refs=[])
followup(agent_id, content, context_refs=[])
wait_for(agent_ids)
wait_for_human(content, context_refs=[])
```

- `spawn` 总是创建新实例。
- `send` 只投递，不创建 turn、不唤醒目标。
- `followup` 显式复用原实例，投递消息并创建新 turn；目标 busy 时零状态变更。
- `wait_for` 和 `wait_for_human` 登记持久化等待，并在安全边界结束当前 turn。

这些命令是统一 Kernel 原语，不代表每个 Agent 都拥有全部权限。Supervisor 获得项目内完整编排能力；其他 Agent 由 Composition Root 按 `agent_type` 注入最小子集。PlotAgent 是通用辅助类型，任何确实需要图片的 Agent 都可以获得创建并等待 PlotAgent 的能力。具体类型与权限见 [已注册 Agent 目录设计](registered-agent-catalog-design.md)。

不新增 `AgentRoleSpec`、`AgentInvocation`、`AgentCompletion`、`SupervisorDecision`、`ApprovalRecord`、`MemoryCandidate` 或专用 `DataAnalysisManifest` 等公共 DTO。必要元数据优先作为存储层内部格式。

## 5. 跨模块不变量

### 5.1 实例与调度

- `agent_id` 使用不透明唯一 ID，不由 name 或树路径拼接。
- 父子关系单独记录为 `parent_id`；路径仅用于展示。
- 就绪队列以 `agent_id` 为元素，Run 只是一次 turn 的审计记录。
- 同一 Agent 同时最多有一个非终态 Run；同类型多个实例可以并行。
- 首版 Scheduler 只有全局并发上限和 FIFO，不做类型配额、权重、优先级或自动重试。
- IDLE、WAITING、WAITING_FOR_HUMAN 不占执行槽；逻辑实例保留到项目归档或删除。

### 5.2 等待与唤醒

- Agent 请求等待时结束当前 turn，持久化等待条件、mailbox 游标和 `context_ref`，然后释放执行槽。
- 子 Agent completion 只有命中已登记等待条件时才唤醒父 Agent；否则只进入 mailbox。
- 用户回复由 Kernel 持久化并创建新 turn；客户端断线、传输超时或服务重启不等于拒绝。
- Run 的 COMPLETED/FAILED 是 turn 终态，不是 Agent 实例终态。

### 5.3 记忆

- 私有记忆绑定 `agent_id`，只有 follow-up 原实例才恢复。
- 项目记忆保存有来源的稳定事实、已验证工程故障与修复经验、决定和结论。
- ReflectionAgent 可以生成全局记忆候选，但批准后才由确定性服务写入全局 store。
- Agent 间交接优先使用正式 Artifact，其次是消息意图，再通过 follow-up 恢复原作者私有记忆。

### 5.4 DataAnalysis 所有权

- DataAgent 负责 EDA、`report.md` 和正式 DataAnalysis 版本，是版本的唯一提交者。
- PlotAgent 是任何 Agent 都可启动的通用绘图 Agent；它只返回图片及说明，不能提交 DataAnalysis。
- ReflectionAgent 先编写 rubric，再逐项评分并给出证据；它不能修改报告或提交新版本。
- EvaluationPolicy 是确定性服务，只按 rubric 的结构化阈值计算 `passed/failed`。
- v2、v3 必须由原 DataAgent 实例在收到 review 后提交，并用 `parent_ref` 连接上一版本。
- 正式版本是不可变 `DataAnalysis/` Bundle，至少包含 `report.md` 与一张可解析图片；旧版本始终保留。

### 5.5 评估协议

- 每次评分绑定精确 rubric 版本，历史评分不被新版本覆盖。
- 新 rubric 版本保存完整快照，只能在父版本基础上追加新 `criterion_id`。
- 不同 rubric 版本的总分不能直接比较；需要比较时，使用同一 rubric 版本按需重评。
- rubric 追加不会自动触发全部历史版本重评。
- Supervisor 可以在 failed 后选择返工、终止或人工确认，但不能把硬门槛改为 passed。

## 6. 关键流程

### 6.1 动态编排

```text
用户请求
  -> 创建或 follow-up root SupervisorAgent
  -> Supervisor 选择已注册 agent_type 并 spawn 独立实例
  -> Kernel 按 FIFO 和并发上限执行
  -> Supervisor 登记 wait_for，结束 turn 并释放执行槽
  -> completion 命中等待条件
  -> Kernel 创建新 turn，恢复 Supervisor 私有记忆
  -> Supervisor 根据结果动态决定下一步
```

### 6.2 DataAnalysis 评审与修订

```text
Supervisor -> DataAgent -> PlotAgent -> DataAnalysis v1
           -> ReflectionAgent -> rubric + review
           -> EvaluationPolicy -> passed / failed

failed 且决定返工
  -> followup 原 data_agent_id，附 v1/rubric/review refs
  -> 原 DataAgent 恢复私有记忆
  -> 提交 DataAnalysis v2，parent_ref = v1
```

### 6.3 用户确认

```text
Supervisor wait_for_human(content, refs)
  -> Kernel 持久化 WAITING_FOR_HUMAN 与 request id
  -> App Server 展示并转交用户回复
  -> Kernel 写入回复、创建新 turn、重新入队原 Supervisor
```

全局记忆候选使用同一流程；未批准候选不能参与其他项目的默认检索。

## 7. 当前代码适配结论

当前 `core/agent_kernel` 已可复用 Agent/Run 分离、命令序列器、mailbox、follow-up、completion outbox 和 generation/CAS。当前工作树还已经完成四项收敛：`AgentMessage` 改为 `source/content/context_refs`；Scheduler 改为按 AgentId FIFO 且只限制活动实例；旧 `core.agent.AgentControl` 及其公开导出已删除；`AgentTypeRegistry`、按类型 create/spawn、Snapshot 的 `agent_type/name` 已加入。这些内容不再列为设计缺口。

仍需在后续实施中收敛的边界：

1. 将当前 `AgentTypeRegistry(agent_type -> AgentSpec)` 收敛为进程内 runtime binding；GraphStore 移除 `AgentRecord.spec`，只保存 `agent_type` 和配置引用，并在恢复时重新解析。
2. `agent_id` 与 name/path 解耦为不透明唯一 ID；当前 Snapshot 的 `agent_type` 与 `name` 继续保留。
3. 进程内 parked wait 和审批 Future 迁为持久化 WAITING / WAITING_FOR_HUMAN。
4. 生产 SessionResources 从 `context_ref` 恢复私有记忆，内存实现只用于测试。
5. App Server、ResearchRuntime、Ideator 和 DataPipeline 的 Agent 生命周期所有权迁入 Kernel；迁移后的调用方不再保留第二条执行路径。
6. 具体业务 Agent 迁入 `src/athena/agents/`，并按 [已注册 Agent 目录设计](registered-agent-catalog-design.md) 静态注册。
7. Agent 正式结果写入 ArtifactStore；DataAnalysis 增加目录 Bundle 与 owner/latest 版本事实。
8. 评估协议改为 append-only 版本链，单个版本对象仍保持不可变。

具体适配方式分别由四份专题设计定义；本文不拆分实施任务，也不修改代码。

## 8. 验收边界

首版只验证公共行为和持久化结果：

- 同一类型可创建多个具有独立身份和私有记忆的实例。
- send 不唤醒，followup 创建新 turn，等待释放执行槽并可跨重启恢复。
- completion 与用户回复最多触发一次匹配的唤醒。
- Supervisor 不能绕过预算、注册表、EvaluationPolicy 或全局记忆批准门禁。
- DataAnalysis v1/v2 由同一 DataAgent 提交，Bundle 含报告和图片，版本 lineage 正确。
- ReflectionAgent 只生成 rubric/review；append 不删除旧条目、不静默覆盖历史评分。
- 使用小型 CSV 和真实模型运行一次手动 smoke test，再根据可复现故障补充校验。

不为尚未观察到的异常建立大规模参数化错误矩阵。Kernel 只保证失败不写坏状态，具体 Agent/工具失败统一反馈给 Supervisor，由其结合结果和预算决定重试、返工、终止或请求用户。

## 9. 确认状态

本文所列架构边界、最小合同、DataAnalysis 所有权、append-only rubric、记忆门禁、持久化等待和精简错误策略均已确认。五份专题文档继续细化这些决定，但不得引入新的公共 DTO 或第二套 Agent 生命周期所有者；新增的静态类型目录与本轮细节修正仍待用户审阅。

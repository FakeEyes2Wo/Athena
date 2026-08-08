# Athena Agent 端到端工作流设计

> 状态：方案 2 已确认，一次性全面迁移
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 范围：唯一运行链路、阶段交接、切换和验收

## 1. 当前判断

当前代码有两组分别可运行的能力：

- 新 Agent Kernel、等待、恢复、权限和 PREPARE 骨架测试已通过。
- 旧 SEARCH、VALIDATE、REPORT 工作流测试已通过。

尚未跑通的是两者之间的统一链路。`ProjectRuntime` 目前只覆盖 CONFIGURE/PREPARE；`ideator/code/report` 仍注册为 `SimpleAgent`；`ResearchRuntime._run_task` 仍拥有另一套生命周期。

现有 PREPARE 也只是结构证明：DataAgent 直接接收 report/figure，未执行真实 EDA 或 spawn PlotAgent；Reflection 结果尚未交给 EvaluationPolicy，当前通过判断仍是“report 非空”；task、DataAnalysis owner/latest 和完整阶段引用尚未一起跨重启恢复。

因此本设计不再扩展局部骨架，而是定义一次性切换后的唯一流程。

## 2. 完成态架构

```text
User / App Server
  -> ProjectRuntime
  -> root SupervisorAgent
  -> AgentKernel
       -> DataAgent -> PlotAgent
       -> ReflectionAgent
       -> IdeatorAgent(s)
       -> CodeAgent(s)
       -> ReportAgent -> PlotAgent
  -> deterministic boundaries
       -> Dataset/Sampling
       -> ArtifactStore
       -> ResearchTree
       -> GitWorkspace/Execution
       -> Evaluator/Comparator/Policies
       -> Project/Global Memory Store
```

`ProjectRuntime` 是 Composition Root 和唯一项目入口。`ResearchRuntime` 若暂时保留，只能把外部协议翻译成 ProjectRuntime 调用；它不创建 Task、不保存 phase、不管理 pause、也不运行 workflow coroutine。

## 3. 项目最小事实

项目 Store 只保存：

```text
project_id
root_supervisor_id
phase
status
task_ref
data_analysis_ref
eval_spec_ref
baseline_experiment_id
sota_experiment_id
validation_ref
final_report_ref
```

Agent/Run/mailbox/wait 仍属于 Kernel；假设和实验仍属于 ResearchTree；正文和大对象仍属于 ArtifactStore。这里不增加公开 `ProjectRuntimeDTO`。

每次权威提交立即耐久化。恢复时按 id/ref 校验，不能按 name 猜测实例，也不能反序列化 Python runner、Task 或 model client。

## 4. 外部调用面

App Server 只需要：

```text
open(project, content, context_refs)
message(project, content, context_refs)
human_reply(request_id, content)
pause(project)
resume(project)
stop(project)
status(project)
```

不再暴露 `SEARCH_START`、`VALIDATE_START`、`REPORT_GENERATE` 等第二套阶段执行命令。阶段由已提交事实投影，Supervisor 在硬门槛满足后继续下一阶段。

## 5. 主流程

```text
CONFIGURE -> PREPARE -> SEARCH -> VALIDATE -> REPORT -> COMPLETED
```

### 5.1 CONFIGURE

确定性 TaskParser 生成 `task_ref`。目标、target、metric、数据授权或评估口径存在权威歧义时，Supervisor 调用 `wait_for_human`。有效 task 是阶段唯一完成条件。

### 5.2 PREPARE

```text
DatasetService
  -> immutable raw/train/validation/final-test refs
  -> training-only profile and sampling refs
Supervisor spawn DataAgent
  -> DataAgent may spawn PlotAgent
  -> DataAnalysis v1
Supervisor spawn ReflectionAgent
  -> rubric -> score -> review
EvaluationPolicy
  -> failed: followup original DataAgent -> v2
  -> passed: accept data_analysis_ref
EvaluatorFactory
  -> append-only EvalSpec -> freeze eval_spec_ref
Supervisor spawn CodeAgent
  -> baseline candidate
trusted evaluator
  -> baseline experiment committed to ResearchTree
```

split 必须在 DataAgent 前冻结。DataAgent 只读 training 数据和不含封存标签的 schema。大数据 EDA 使用三个不同且记录的 seed；报告区分稳定和样本敏感结论。EDA 样本不能进入训练或评估。

PREPARE 完成条件：已批准 DataAnalysis、冻结 EvalSpec、成功 baseline。

### 5.3 SEARCH

```text
Supervisor spawn one-or-more IdeatorAgent
  -> hypotheses + evidence refs
deterministic ranker selects candidates
Supervisor spawn CodeAgent per selected hypothesis
  -> candidate diff + run refs
trusted evaluator -> EvalResult
Comparator + SearchDecisionPolicy
  -> accept / reject / stop
ResearchTree commits hypothesis, experiment and SOTA
Supervisor repeats within budget
```

真实失败需要修复时 follow-up 原 CodeAgent；需要独立方案时创建新实例。旧 `SearchLoop` 不再创建或持有 Agent，只可拆出无状态 ranking/comparison/policy helper。

SEARCH 完成条件：预算或策略停止，且存在成功 SOTA。

### 5.4 VALIDATE

Supervisor 基于冻结 SOTA 创建 CodeAgent 完成预先声明的 ablation。受信任 Validator 执行并提交结果。随后对同一冻结 commit 和 evaluator 执行一次 final-test。

final-test 输出不能反馈给 CodeAgent 修改代码。若执行失败，可以修复执行环境后重跑同一冻结内容；不得借失败改变模型方案。

VALIDATE 完成条件：要求的 ablation 已记录，final-test 恰好一个有效结果。

### 5.5 REPORT

```text
Supervisor spawn ReportAgent
  context_refs = approved evidence refs
ReportAgent may spawn PlotAgent
  -> FinalReport v1
Supervisor spawn ReflectionAgent
  -> report rubric + score + review
EvaluationPolicy
  -> failed: followup original ReportAgent -> v2
  -> passed: accept final_report_ref
```

ReportAgent 不运行实验、不修改指标、不补造证据。旧版本保留，修订直接回写工作区草稿，但正式提交生成新 Bundle。

REPORT 完成条件：最终报告通过门槛，且引用均可追溯到已批准 Artifact。

### 5.6 COMPLETED

已验证事实和工程修复可以进入项目记忆。跨项目候选必须 `wait_for_human`；批准后由确定性服务写全局索引。全局记忆不是完成主报告的前置条件。

## 6. Agent 与确定性服务

| Agent | 只负责 | 不负责 |
|---|---|---|
| Supervisor | 动态编排和返工选择 | 状态存储、评分、Git 提交 |
| Data | EDA、DataAnalysis 版本 | split、评审分数 |
| Plot | 图片、图注、观察 | 调用方报告版本 |
| Reflection | rubric、证据评分、review | 修改被评 Artifact |
| Ideator | 假设生成和修订 | 排名数值、ResearchTree 提交 |
| Code | 代码候选和基于失败的修复 | evaluator、SOTA 接受 |
| Report | 最终报告版本 | 重跑实验、改变指标 |

Dataset、Sampling、Ranker、Comparator、EvaluationPolicy、Validator、Git、ArtifactStore 和 ResearchTree 都是确定性边界，不注册为 Agent。

## 7. Artifact 交接

| 阶段 | 必须提交的引用 |
|---|---|
| CONFIGURE | task |
| PREPARE | split/profile、DataAnalysis、rubric/review、EvalSpec、baseline |
| SEARCH | hypothesis、diff、logs、EvalResult、SOTA |
| VALIDATE | ablation、frozen final-test |
| REPORT | FinalReport、rubric/review |

AgentMessage 只携带短 `content` 和这些 refs。Agent completion 不复制业务对象。

## 8. 等待、失败和恢复

- `wait_for`/`wait_for_human` 提交等待后立即结束 turn。
- 正常完成或成功进入等待后才提交 mailbox cursor。
- Run 失败形成 RunSummary；Kernel 不自动重试。
- Supervisor 只能选择 follow-up、新实例、请求用户或停止，不能把 failed gate 改成 passed。
- 重启恢复 Agent、Run、wait、mailbox、phase 和 refs；RUNNING 的旧调用栈不恢复。
- pause 停止新派发，stop 终止项目；两者都不删除已提交 Artifact、mailbox 或 wait 审计。

## 9. 一次性切换内容

以下内容作为一个迁移单元完成：

1. 用真实 IdeatorAgent、CodeAgent、ReportAgent factory 替换 `SimpleAgent`。
2. Data/Plot/Reflection 从确定性占位实现接入真实数据、绘图和模型评审工具。
3. EvaluationPolicy 读取 Reflection score/rubric，而不是从 report 正文重新推断通过状态。
4. Supervisor 按阶段事实驱动完整流程，不在对象字段中保存权威进度。
5. 将现有 SearchLoop 拆为 Agent 调用和确定性 helper，移除其 Agent 所有权。
6. Validator 和 Reporter 改为由 Kernel Agent 流程触发。
7. App Server 改为只调用 ProjectRuntime。
8. 删除 `ResearchRuntime._run_task`、旧 AgentTask 和直接调用业务 Agent 的 Pipeline 路径。
9. 统一持久化 ProjectStore；task、Bundle lineage 和阶段 refs 与每次 Kernel/项目命令一起耐久化。

内部实现可以按上述顺序推进，但切换验收前不把任一步称为完整新架构，也不建立新旧状态双写。

## 10. 验收

必须同时通过：

1. 七类 factory 都创建独立实例，无 `SimpleAgent` 注册。
2. 同父同名实例有不同 `agent_id`、memory 和 Run。
3. DataAgent -> PlotAgent -> ReflectionAgent -> 原 DataAgent v2 流程可运行。
4. IdeatorAgent -> CodeAgent -> evaluator -> ResearchTree SOTA 流程可运行。
5. VALIDATE 使用冻结 SOTA，final-test 恰好一次。
6. ReportAgent -> ReflectionAgent -> 原 ReportAgent v2 流程可运行。
7. human wait、pause 和进程重启不丢状态或重复 completion。
8. 全局记忆在批准前不能被其他项目检索。
9. 源码中不存在第二套 Agent lifecycle owner。
10. 一个真实小型 CSV 完成全流程，最终报告可以追溯到 task、DataAnalysis、EvalSpec、SOTA、validation、diff 和 logs。

## 11. 明确不做

本次不设计长期兼容桥、双 Runtime 同步、复杂错误码、自动降级、动态类型注册、向量记忆库或复杂调度配额。只有真实端到端运行暴露的问题才增加新的检查。

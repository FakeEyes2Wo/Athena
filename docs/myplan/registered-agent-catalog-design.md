# Athena 已注册 Agent 目录设计

> 状态：一次性迁移专题设计
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)

## 1. 准入规则

只有需要独立推理上下文、私有记忆、工具权限或并发身份的职责才注册为 Agent。状态迁移、排序数值、阈值、预算、文件边界、数据切分、Git 提交和 Artifact 写入规则保留为确定性 Service。

## 2. 七个类型

| `agent_type` | 单一职责 | 主要结果 |
|---|---|---|
| `supervisor` | 动态编排项目 | 最终结果引用、人工请求 |
| `data` | EDA 和 DataAnalysis 版本 | DataAnalysis Bundle |
| `plot` | 通用绘图 | 图片、图注、观察 |
| `reflection` | rubric、证据评分和 review | Review Bundle |
| `ideator` | 假设生成、辩论和修订 | Hypothesis Artifact |
| `code` | 代码候选和真实失败修复 | diff、logs、run refs |
| `report` | 最终报告和修订 | FinalReport Bundle |

一个类型可以创建多个独立实例。judge、debater、fixer 等只是 name 或初始任务差异，不增加新类型。

## 3. 实例隔离

Registry 保存 `agent_type -> factory`。factory 每次按 `agent_id` 创建新的 runner/Agent 对象；同类型实例不得共享可变字段或私有 memory。

允许共享：

- 无状态 service；
- model/provider 配置；
- 连接池和只读工具定义。

不能共享：

- turn 进度；
- conversation/context；
- 当前报告或实验版本；
- 子 Agent 列表。

## 4. 最小工具权限

| 类型 | 允许 spawn | 其他编排能力 |
|---|---|---|
| supervisor | data、plot、reflection、ideator、code、report | send、followup、wait_for、wait_for_human |
| data | plot | followup 自己的 Plot、wait_for |
| ideator | ideator、reflection、plot | followup 自己的子实例、wait_for |
| code | plot | followup 自己的 Plot、wait_for |
| report | plot | followup 自己的 Plot、wait_for |
| reflection | 无 | 无 |
| plot | 无 | 无 |

需要用户判断时，业务 Agent 返回问题 Artifact，由 Supervisor 统一进入 human wait。业务 Agent 不能动态注册类型、修改 Scheduler、关闭任意非子树实例或直接写全局记忆。

## 5. 确定性边界

以下对象不注册为 Agent：

| 能力 | 目标归属 |
|---|---|
| Search ACCEPT/REJECT/STOP | SearchDecisionPolicy |
| score aggregation / threshold | EvaluationPolicy |
| ranking / Elo / proximity | 普通算法 |
| Dataset split / sampling | DatasetService |
| timeout / stall | ExecutionMonitor |
| experiment/SOTA commit | ResearchTree service |
| Git commit/worktree | GitWorkspace service |
| global memory write | approved MemoryService |

## 6. 当前切换要求

当前 data/plot/reflection 是确定性骨架，ideator/code/report 是 `SimpleAgent` 占位。一次性切换必须：

1. 为七类注册真实 factory；
2. 将现有 Ideator、Code 和 Reporter 能力迁入对应 Agent；
3. 删除旧私有 thread/history 和 Pipeline 直调路径；
4. 将当前“多数类型获得全部工具”的投影收窄为上表；
5. 保留现有确定性 ranking、evaluation、validation 和 storage 服务。

## 7. 验收

- Registry 恰好注册七类生产 factory；
- 同类型两个实例不共享业务对象或记忆；
- Code 修复 follow-up 原实例，不创建 FixAgent；
- PlotAgent 可被多个业务类型创建，但不能提交调用方版本；
- ReflectionAgent 不能修改被评 Artifact；
- 未注册类型被拒绝；
- SimpleAgent 不再出现在生产 Composition Root。
